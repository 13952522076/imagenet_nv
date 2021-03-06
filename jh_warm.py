import argparse, os, shutil, time, warnings

from fastai.transforms import *
from fastai.dataset import *
from fastai.fp16 import *
from fastai.conv_learner import *
from pathlib import *

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import models_bak
from distributed import DistributedDataParallel as DDP

model_names = sorted(name for name in models_bak.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models_bak.__dict__[name]))
# print(model_names)

def get_parser():
    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    parser.add_argument('data', metavar='DIR', help='path to dataset')
    parser.add_argument('--save-dir', type=str, default=Path.home()/'imagenet_training',
                        help='Directory to save logs and models.')
    parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                        choices=model_names, help='model architecture'),
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('-b', '--batch-size', default=256, type=int,
                        metavar='N', help='mini-batch size (default: 256)')
    parser.add_argument('--fp16', action='store_true', help='Run model fp16 mode.')
    parser.add_argument('--dist-url', default='file://sync.file', type=str,
                        help='url used to set up distributed training')
    parser.add_argument('--dist-backend', default='nccl', type=str, help='distributed backend')
    parser.add_argument('--world-size', default=1, type=int,
                        help='Number of GPUs to use. Can either be manually set ' +
                        'or automatically set by using \'python -m multiproc\'.')
    parser.add_argument('--rank', default=0, type=int,
                        help='Used for multi-process training. Can either be manually set ' +
                        'or automatically set by using \'python -m multiproc\'.')
    return parser

class TorchModelData(ModelData):
    def __init__(self, path, trn_dl, val_dl, aug_dl=None):
        super().__init__(path, trn_dl, val_dl)
        self.aug_dl = aug_dl


__imagenet_pca = {
    'eigval': torch.Tensor([0.2175, 0.0188, 0.0045]),
    'eigvec': torch.Tensor([
        [-0.5675,  0.7192,  0.4009],
        [-0.5808, -0.0045, -0.8140],
        [-0.5836, -0.6948,  0.4203],
    ])
}

# Lighting data augmentation take from here - https://github.com/eladhoffer/convNet.pytorch/blob/master/preprocess.py
class Lighting(object):
    """Lighting noise(AlexNet - style PCA - based noise)"""

    def __init__(self, alphastd, eigval, eigvec):
        self.alphastd = alphastd
        self.eigval = eigval
        self.eigvec = eigvec

    def __call__(self, img):
        if self.alphastd == 0:
            return img

        alpha = img.new().resize_(3).normal_(0, self.alphastd)
        rgb = self.eigvec.type_as(img).clone()\
            .mul(alpha.view(1, 3).expand(3, 3))\
            .mul(self.eigval.view(1, 3).expand(3, 3))\
            .sum(1).squeeze()
        return img.add(rgb.view(3, 1, 1).expand_as(img))

def torch_loader(data_path, size, bs, min_scale=0.08):
    # Data loading code
    traindir = os.path.join(data_path, 'train')
    valdir = os.path.join(data_path, 'val')
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

    train_tfms = transforms.Compose([
        transforms.RandomResizedCrop(size, (min_scale,1.)),
        transforms.RandomHorizontalFlip(),
        #transforms.ColorJitter(.3,.3,.3),
        transforms.ToTensor(),
        #Lighting(0.1, __imagenet_pca['eigval'], __imagenet_pca['eigvec']),
        normalize,
    ])
    train_dataset = datasets.ImageFolder(traindir, train_tfms)
    train_sampler = (torch.utils.data.distributed.DistributedSampler(train_dataset) if args.distributed else None)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=bs, shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)

    val_tfms = transforms.Compose([
        transforms.Resize(int(size*1.14)),
        transforms.CenterCrop(size),
        transforms.ToTensor(),
        normalize,
    ])
    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, val_tfms), batch_size=bs*2, shuffle=False, num_workers=args.workers, pin_memory=True)


    aug_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, train_tfms), batch_size=bs, shuffle=False, num_workers=args.workers, pin_memory=True)

    train_loader = DataPrefetcher(train_loader)
    val_loader = DataPrefetcher(val_loader)
    aug_loader = DataPrefetcher(aug_loader)
    data = TorchModelData(data_path, train_loader, val_loader, aug_loader)
    return data, train_sampler

# Seems to speed up training by ~2%
class DataPrefetcher():
    def __init__(self, loader, stop_after=None):
        self.loader = loader
        self.dataset = loader.dataset
        self.stream = torch.cuda.Stream()
        self.stop_after = stop_after
        self.next_input = None
        self.next_target = None

    def __len__(self):
        return len(self.loader)
    
    def preload(self):
        try:
            self.next_input, self.next_target = next(self.loaditer)
        except StopIteration:
            self.next_input = None
            self.next_target = None
            return
        with torch.cuda.stream(self.stream):
            self.next_input = self.next_input.cuda(async=True)
            self.next_target = self.next_target.cuda(async=True)

    def __iter__(self):
        count = 0
        self.loaditer = iter(self.loader)
        self.preload()
        while self.next_input is not None:
            torch.cuda.current_stream().wait_stream(self.stream)
            input = self.next_input
            target = self.next_target
            self.preload()
            count += 1
            yield input, target
            if type(self.stop_after) is int and (count > self.stop_after):
                break


def top5(output, target):
    """Computes the precision@k for the specified values of k"""
    top5 = 5
    batch_size = target.size(0)
    _, pred = output.topk(top5, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))
    correct_k = correct[:top5].view(-1).float().sum(0, keepdim=True)
    return correct_k.mul_(1.0 / batch_size)

# Creating a custom logging callback. Fastai logger actually hurts performance by writing every batch.
class ImagenetLoggingCallback(Callback):
    def __init__(self, save_path, print_every=50):
        super().__init__()
        self.save_path=save_path
        self.print_every=print_every
    def on_train_begin(self):
        self.batch = 0
        self.epoch = 0
        self.f = open(self.save_path, "a", 1)
        self.log("\ton_train_begin")
    def on_epoch_end(self, metrics):
        log_str = f'\tEpoch:{self.epoch}\ttrn_loss:{self.last_loss}'
        for (k,v) in zip(['val_loss', 'acc', 'top5', ''], metrics): log_str += f'\t{k}:{v}'
        self.log(log_str)
        self.epoch += 1
    def on_batch_end(self, metrics):
        self.last_loss = metrics
        self.batch += 1
        if self.batch % self.print_every == 0:
            self.log(f'Epoch: {self.epoch} Batch: {self.batch} Metrics: {metrics}')
    def on_train_end(self):
        self.log("\ton_train_end")
        self.f.close()
    def log(self, string):
        self.f.write(time.strftime("%Y-%m-%dT%H:%M:%S")+"\t"+string+"\n")

# Logging + saving models
def save_args(name, save_dir):
    if (args.rank != 0) or not args.save_dir: return {}

    log_dir = f'{save_dir}/training_logs'
    os.makedirs(log_dir, exist_ok=True)
    return {
        'best_save_name': f'{name}_best_model',
        'cycle_save_name': f'{name}',
        'callbacks': [
            ImagenetLoggingCallback(f'{log_dir}/{name}_log.txt')
        ]
    }

def save_sched(sched, save_dir):
    if (args.rank != 0) or not args.save_dir: return {}
    log_dir = f'{save_dir}/training_logs'
    sched.save_path = log_dir
    sched.plot_loss()
    sched.plot_lr()

def update_model_dir(learner, base_dir):
    learner.tmp_path = f'{base_dir}/tmp'
    os.makedirs(learner.tmp_path, exist_ok=True)
    learner.models_path = f'{base_dir}/models'
    os.makedirs(learner.models_path, exist_ok=True)

def fit(learner, name, lr, cycle_len, sampler, wds, clr=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        print(f'\n-- {name} --\n')
        sargs = save_args(name, args.save_dir)
        learner.fit(lr, 1, cycle_len=cycle_len, sampler=sampler, wds=wds, use_clr=clr, loss_scale=1024, **sargs)

cudnn.benchmark = True
args = get_parser().parse_args()
print('Running script with args:', args)

def main():
    args.distributed = args.world_size > 1
    args.gpu = 0
    if args.distributed:
        args.gpu = args.rank % torch.cuda.device_count()
        torch.cuda.set_device(args.gpu)
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url, world_size=args.world_size)

    if args.fp16: assert torch.backends.cudnn.enabled, "fp16 mode requires cudnn backend to be enabled."

    model = models_bak.__dict__[args.arch]().cuda()
    if args.distributed: model = DDP(model)

    data, train_sampler = torch_loader(f'{args.data}-sz/160', 128, 256)
    learner = Learner.from_model_data(model, data)
    learner.crit = F.cross_entropy
    learner.metrics = [accuracy, top5]
    if args.fp16: learner.half()
    wd=2e-5
    update_model_dir(learner, args.save_dir)
    fit(learner, '1', 0.03, 1, train_sampler, wd)

    data, train_sampler = torch_loader(f'{args.data}-sz/320', 128, 256)
    learner.set_data(data)
    fit(learner, '3', 1e-1, 1, train_sampler, wd)

    data, train_sampler = torch_loader(args.data, 128, 256)
    learner.set_data(data)
    fit(learner, '3', 1e-1, 1, train_sampler, wd)
    print('Finished!')


main()

