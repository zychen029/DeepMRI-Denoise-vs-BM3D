import os
import time
import logging
import itertools
import math
import numpy as np
import random
from PIL import Image
import importlib
from tensorboardX import SummaryWriter
from matplotlib import pyplot as plt
import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
from torch.autograd import Variable
from torch.optim.lr_scheduler import CosineAnnealingLR
import torchvision
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from collections import OrderedDict
import importlib
import sys
sys.path.append("..")
from dataloader.dataset import TrainSet #, TestSet , TestSet_multi, Urban100, Sun80
from utils import util, calculate_PSNR_SSIM
from models.modules import define_G
from models.losses import PerceptualLoss, AdversarialLoss
from dataloader import DistIterSampler, create_dataloader
from skimage.metrics import peak_signal_noise_ratio,structural_similarity,normalized_root_mse


class Trainer(object):
    def __init__(self, args):
        super(Trainer, self).__init__()
        self.args = args
        self.augmentation = args.data_augmentation
        self.device = torch.device('cuda' if len(args.gpu_ids) != 0 else 'cpu')
        args.device = self.device

        ## init dataloader
        if args.phase == 'train':
            # In train.py, the default value for the --trainset parameter passed through args is set to 'Trainset', which corresponds to：
            # Specify the use of dataset.py in the dataloader folder, creating an instance trainset_ of the Trainset class.
            trainset_ = getattr(importlib.import_module('dataloader.dataset'), args.trainset, None)
            if args.modal == 'ALL':
                self.args.modal = 'T1'
                self.train_dataset1 = trainset_(self.args)
                self.args.modal = 'T2'
                self.train_dataset2 = trainset_(self.args)
                self.args.modal = 'FLAIR'
                self.train_dataset3 = trainset_(self.args)
                self.train_dataset = self.train_dataset1 + self.train_dataset2 + self.train_dataset3
            else:
                self.train_dataset = trainset_(self.args)
            ## Initialize the distributed training data loader.
            if args.dist: 
                dataset_ratio = 1
                train_sampler = DistIterSampler(self.train_dataset, args.world_size, args.rank, dataset_ratio)
                self.train_dataloader = create_dataloader(self.train_dataset, args, train_sampler)
            else:
                self.train_dataloader = DataLoader(self.train_dataset, batch_size=args.batch_size, num_workers=args.num_workers, shuffle=True)

        # In train.py, the default value for the --testset parameter passed through args is set to 'Testset', which corresponds to:
        # Specify the use of dataset.py in the dataloader folder, creating an instance testset_ of the Testset class.
        testset_ = getattr(importlib.import_module('dataloader.dataset'), args.testset, None)
        self.test_dataset = testset_(self.args)
        self.test_dataloader = DataLoader(self.test_dataset, batch_size=1, num_workers=args.num_workers, shuffle=False)

        ## init network
        self.net = define_G(args)
        if args.resume:
            self.load_networks('net', self.args.resume)

        if args.rank <= 0:
            logging.info('----- generator parameters: %f -----' % (sum(param.numel() for param in self.net.parameters()) / (10**6)))

        ## init loss and optimizer
        if args.phase == 'train':
            if args.rank <= 0:
                logging.info('init criterion and optimizer...')
            g_params = [self.net.parameters()]

            self.criterion_mse = nn.MSELoss().to(self.device)
            if args.loss_mse:
                self.criterion_mse = nn.MSELoss().to(self.device)
                self.lambda_mse = args.lambda_mse
                if args.rank <= 0:
                    logging.info('  using mse loss...')

            if args.loss_l1:
                self.criterion_l1 = nn.L1Loss().to(self.device)
                self.lambda_l1 = args.lambda_l1
                if args.rank <= 0:
                    logging.info('  using l1 loss...')

            if args.loss_adv:
                self.criterion_adv = AdversarialLoss(gpu_ids=args.gpu_ids, dist=args.dist, gan_type=args.gan_type,
                                                             gan_k=1, lr_dis=args.lr_D, train_crop_size=40)
                self.lambda_adv = args.lambda_adv
                if args.rank <= 0:
                    logging.info('  using adv loss...')

            # The original project used the Adam optimizer, and here it has been replaced with the AdamW optimizer.
            self.optimizer_G = torch.optim.AdamW(itertools.chain.from_iterable(g_params), lr=args.lr, weight_decay=args.weight_decay)  
            self.scheduler = CosineAnnealingLR(self.optimizer_G, T_max=500)  # T_max=args.max_iter

            if args.resume_optim:
                self.load_networks('optimizer_G', self.args.resume_optim)
            if args.resume_scheduler:
                self.load_networks('scheduler', self.args.resume_scheduler)

    def set_learning_rate(self, optimizer, epoch):
        current_lr = self.args.lr * 0.3**(epoch//550)
        optimizer.param_groups[0]['lr'] = current_lr
        if self.args.rank <= 0:
            logging.info('current_lr: %f' % (current_lr))

    def vis_results(self, epoch, i, images):
        for j in range(min(images[0].size(0), 5)): # Iterate over the first 5 images in each batch
            # Concatenate to create the filename for saving the result image, in the format 'vis_current_epoch_current_iteration_image_number.jpg'
            save_name = os.path.join(self.args.vis_save_dir, 'vis_%d_%d_%d.jpg' % (epoch, i, j))
            temps = []                  # Temporary list to store data for the current image across different channels
            for imgs in images:            # Iterate over each image in the images list, extracting data for the j-th channel
                temps.append(imgs[j])       # Stack data in temps along a new dimension for a tensor with all channel data
            temps = torch.stack(temps)       # Save the result image to the specified path using the torchvision utility
            torchvision.utils.save_image(temps, save_name)

    def set_requires_grad(self, nets, requires_grad=False):
        if not isinstance(nets, list):
            nets = [nets]
        for net in nets:
            if net is not None:
                for param in net.parameters():
                    param.requires_grad = requires_grad

    def prepare(self, batch_samples):
        for key in batch_samples.keys():
            if 'name' not in key and 'pad_nums' not in key:
                batch_samples[key] = Variable(batch_samples[key].to(self.device), requires_grad=False)
        return batch_samples

    def train(self):
        if self.args.rank <= 0:
            logging.info('training on  ...' + self.args.dataset)
            logging.info('%d training samples' % (self.train_dataset.__len__()))
            logging.info('the init lr: %f'%(self.args.lr))
        steps = 0
        self.net.train()

        if self.args.use_tb_logger:
            if self.args.rank <= 0:
                tb_logger = SummaryWriter(log_dir='tb_logger/' + self.args.name)

        self.best_psnr = 0
        self.augmentation = False  # disenable data augmentation to warm up the encoder
        for i in range(self.args.start_iter, self.args.max_iter):
            self.scheduler.step()
            logging.info('current_lr: %f' % (self.optimizer_G.param_groups[0]['lr']))
            t0 = time.time()
            for j, batch_samples in enumerate(self.train_dataloader):
                log_info = 'epoch:%03d step:%04d  ' % (i, j)

                ## prepare data
                batch_samples = self.prepare(batch_samples)
                images = batch_samples['images']
                labels = batch_samples['labels']

                ## forward
                output = self.net(images)

                ## optimization
                loss = 0
                self.optimizer_G.zero_grad()

                if self.args.loss_mse:
                    mse_loss = self.criterion_mse(output,labels)
                    mse_loss = mse_loss * self.lambda_mse
                    loss += mse_loss
                    log_info += 'mse_loss:%.06f ' % (mse_loss.item())

                if self.args.loss_l1: # Default is L1 loss.
                    l1_loss = self.criterion_l1(output, labels)
                    l1_loss = l1_loss * self.lambda_l1
                    loss += l1_loss
                    log_info += 'l1_loss:%.06f ' % (l1_loss.item())

                if self.args.loss_adv:
                    adv_loss, d_loss = self.criterion_adv(output, labels)
                    adv_loss = adv_loss * self.lambda_adv
                    loss += adv_loss
                    log_info += 'adv_loss:%.06f ' % (adv_loss.item())
                    log_info += 'd_loss:%.06f ' % (d_loss.item())

                log_info += 'loss_sum:%f ' % (loss.item())
                loss.backward()
                self.optimizer_G.step()

                ## print information
                if j % self.args.log_freq == 0:
                    t1 = time.time()
                    log_info += 'aug:%s ' % str(self.augmentation)
                    log_info += '%4.6fs/batch' % ((t1-t0)/self.args.log_freq)
                    if self.args.rank <= 0:
                        logging.info(log_info)
                    t0 = time.time()

                ## Visualization: Call the vis_results function for visualization at regular intervals
                if j % self.args.vis_freq == 0:
                    # Construct a list containing input images, output, and true labels
                    vis_temps = [batch_samples['images'], output, batch_samples['labels']]
                    self.vis_results(i, j, vis_temps)  # Call the vis_results function to save visualization results
                
                ## write tb_logger
                if self.args.use_tb_logger:
                    if steps % self.args.vis_step_freq == 0:
                        if self.args.rank <= 0:
                            if self.args.loss_mse:
                                tb_logger.add_scalar('mse_loss', mse_loss.item(), steps)
                            if self.args.loss_l1:
                                tb_logger.add_scalar('l1_loss', l1_loss.item(), steps)
                            if self.args.loss_adv:
                                if i > 5:
                                    tb_logger.add_scalar('adv_loss', adv_loss.item(), steps)
                                    tb_logger.add_scalar('d_loss', d_loss.item(), steps)

                steps += 1

            ## save networks
            if i % self.args.save_epoch_freq == 0:
                if self.args.rank <= 0:
                    logging.info('Saving state, epoch: %d iter:%d' % (i, 0))
                    self.save_networks('net', i)
                    self.save_networks('optimizer_G', i)
                    self.save_networks('scheduler', i)

            if not self.args.loss_adv:
                if i > 200 and self.args.modal != 'ALL':
                    self.args.phase = 'eval'
                    psnr, ssim,psnr_std,ssim_std = self.evaluate()
                    logging.info('Mean: psnr:%.06f   ssim:%.06f ' % (psnr, ssim))
                    logging.info('Std : psnr:%.06f   ssim:%.06f ' % (psnr_std,ssim_std))
                    if psnr > self.best_psnr:
                        self.best_psnr = psnr
                        if self.args.rank <= 0:
                            logging.info('best_psnr:%.06f ' % (self.best_psnr))
                            logging.info('Saving state, epoch: %d iter:%d' % (i, 0))
                            self.save_networks('net', 'best')
                            self.save_networks('optimizer_G', 'best')
                            self.save_networks('scheduler', 'best')
                        ## start data augmentation
                        if i > 30:
                            self.augmentation = self.args.data_augmentation
                    self.args.phase = 'train'

        ## end of training
        if self.args.rank <= 0:
            # tb_logger.close()
            self.save_networks('net', 'final')
            logging.info('The training stage on %s is over!!!' % (self.args.dataset))


    def test(self):
        self.net.eval()
        logging.info('start testing...')
        logging.info('%d testing samples' % (self.test_dataset.__len__()))

        PSNR = []
        SSIM = []
        predictions = np.zeros([648,256,256])
        with torch.no_grad():
            for batch, batch_samples in enumerate(self.test_dataloader):
                batch_samples = self.prepare(batch_samples)
                images = batch_samples['images']
                labels = batch_samples['labels']
                output = self.net(images)
                
                # Construct a list containing input images, output, and true labels
                if batch == 26 or batch == 28:
                    test_temps = [images, output, labels]
                    self.test_results(batch, test_temps)  # Save the visual results when the batch is 26 or 28 (randomly selected)
                
                output = torch.clip(output,0,1)
                output_img = output.detach().cpu().numpy().astype(np.float32)[0][0]
                gt = labels.detach().cpu().numpy().astype(np.float32)[0][0]
                predictions[batch] = output_img

                psnr = peak_signal_noise_ratio(output_img, gt,data_range=1)
                ssim = structural_similarity(output_img, gt,data_range=1)
                PSNR.append(psnr)
                SSIM.append(ssim)
                logging.info('psnr: %.4f    ssim: %.4f' % (psnr, ssim))
        # np.save(f'M4RawV1.0_experiment/predictions/exp_result/finetune-{self.net_name}-{self.args.modal}.npy',predictions)
        psnr_mean = np.mean(PSNR)
        ssim_mean = np.mean(SSIM)
        psnr_std = np.std(PSNR)
        ssim_std = np.std(SSIM)
        logging.info('-------- average Mean PSNR: %.04f,  SSIM: %.04f' % (psnr_mean, ssim_mean))
        logging.info('-------- average Std  PSNR: %.04f,  SSIM: %.04f' % (psnr_std, ssim_std))

    def test_results(self, step, images):
        for j in range(min(images[0].size(0), 5)):
            save_name = os.path.join(self.args.test_save_dir, 'test_%d_%d.jpg' % (step, j))
            temps = []
            for imgs in images:
                temps.append(imgs[j])
            temps = torch.stack(temps)
            torchvision.utils.save_image(temps, save_name)
        
    def evaluate(self):
        self.net.eval()
        logging.info('start testing...')
        logging.info('%d testing samples' % (self.test_dataset.__len__()))

        PSNR = []
        SSIM = []
        with torch.no_grad():
            for batch, batch_samples in enumerate(self.test_dataloader):
                # batch_samples = self.prepare(batch_samples)
                images = batch_samples['images']
                labels = batch_samples['labels']
                output = self.net(images)
                output = torch.clip(output,0,1)
                output_img = output.detach().cpu().numpy().astype(np.float32)[0][0]
                gt = labels.detach().cpu().numpy().astype(np.float32)[0][0]
                psnr = peak_signal_noise_ratio(output_img, gt,data_range=1)
                ssim = structural_similarity(output_img, gt,data_range=1)
                PSNR.append(psnr)
                SSIM.append(ssim)

        psnr_mean = np.mean(PSNR)
        ssim_mean = np.mean(SSIM)
        psnr_std = np.std(PSNR)
        ssim_std = np.std(SSIM)

        return psnr_mean, ssim_mean,psnr_std,ssim_std

    def save_image(self, tensor, path):
        img = Image.fromarray(((tensor/2.0 + 0.5).data.cpu().numpy()*255).transpose((1, 2, 0)).astype(np.uint8))
        img.save(path)

    def load_networks(self, net_name, resume, strict=True):
        load_path = resume
        network = getattr(self, net_name)
        if isinstance(network, nn.DataParallel) or isinstance(network, DistributedDataParallel):
            network = network.module
        load_net = torch.load(load_path, map_location=torch.device(self.device))
        load_net_clean = OrderedDict()  # remove unnecessary 'module.'
        for k, v in load_net.items():
            if k.startswith('module.'):
                load_net_clean[k[7:]] = v
            else:
                load_net_clean[k] = v
        if 'optimizer' or 'scheduler' in net_name:
            network.load_state_dict(load_net_clean)
        else:
            network.load_state_dict(load_net_clean, strict=strict)
        del load_net_clean

    def save_networks(self, net_name, epoch):
        network = getattr(self, net_name)
        save_filename = '{}_{}.pth'.format(net_name, epoch)
        save_path = os.path.join(self.args.snapshot_save_dir, save_filename)
        if isinstance(network, nn.DataParallel) or isinstance(network, DistributedDataParallel):
            network = network.module
        state_dict = network.state_dict()
        if not 'optimizer' and not 'scheduler' in net_name:
            for key, param in state_dict.items():
                state_dict[key] = param.cpu()
        torch.save(state_dict, save_path)
