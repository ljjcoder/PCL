import pickle
import json
import torch
import numpy as np
import pandas as pd
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from torch.cuda.amp import autocast, GradScaler
from collections import Counter
import os
import contextlib
from train_utils import AverageMeter

from .flexmatch_utils import consistency_loss, Get_Scalar
from train_utils import ce_loss, wd_loss, EMA, Bn_Controller

from sklearn.metrics import *
from copy import deepcopy


class FlexMatch:
    def __init__(self, net_builder, num_classes, ema_m, T, p_cutoff, lambda_u, \
                 hard_label=True, t_fn=None, p_fn=None, it=0, num_eval_iter=1000, tb_log=None, logger=None):
        """
        class Flexmatch contains setter of data_loader, optimizer, and model update methods.
        Args:
            net_builder: backbone network class (see net_builder in utils.py)
            num_classes: # of label classes 
            ema_m: momentum of exponential moving average for eval_model
            T: Temperature scaling parameter for output sharpening (only when hard_label = False)
            p_cutoff: confidence cutoff parameters for loss masking
            lambda_u: ratio of unsupervised loss to supervised loss
            hard_label: If True, consistency regularization use a hard pseudo label.
            it: initial iteration count
            num_eval_iter: freqeuncy of iteration (after 500,000 iters)
            tb_log: tensorboard writer (see train_utils.py)
            logger: logger (see utils.py)
        """

        super(FlexMatch, self).__init__()

        # momentum update param
        self.loader = {}
        self.num_classes = num_classes
        self.ema_m = ema_m

        # create the encoders
        # network is builded only by num_classes,
        # other configs are covered in main.py

        self.model = net_builder(num_classes=num_classes)
        self.ema_model = None

        self.num_eval_iter = num_eval_iter
        self.t_fn = Get_Scalar(T)  # temperature params function
        self.p_fn = Get_Scalar(p_cutoff)  # confidence cutoff function
        self.lambda_u = lambda_u
        self.tb_log = tb_log
        self.use_hard_label = hard_label

        self.optimizer = None
        self.scheduler = None

        self.it = 0
        self.logger = logger
        self.print_fn = print if logger is None else logger.info

        self.bn_controller = Bn_Controller()
        self.criterion = nn.CrossEntropyLoss().cuda()

    def set_data_loader(self, loader_dict):
        self.loader_dict = loader_dict
        self.print_fn(f'[!] data loader keys: {self.loader_dict.keys()}')

    def set_dset(self, dset):
        self.ulb_dset = dset

    def set_optimizer(self, optimizer, scheduler=None):
        self.optimizer = optimizer
        self.scheduler = scheduler

    def train(self, args, logger=None):

        ngpus_per_node = torch.cuda.device_count()

        # EMA Init
        self.model.train()
        self.ema = EMA(self.model, self.ema_m)
        self.ema.register()
        if args.resume == True:
            self.ema.load(self.ema_model)

        # p(y) based on the labeled examples seen during training
        dist_file_name = r"./data_statistics/" + args.dataset + '_' + str(args.num_labels) + '.json'
        if args.dataset.upper() == 'IMAGENET':
            p_target = None
        else:
            with open(dist_file_name, 'r') as f:
                p_target = json.loads(f.read())
                p_target = torch.tensor(p_target['distribution'])
                p_target = p_target.cuda(args.gpu)
            # print('p_target:', p_target)

        p_model = None

        # for gpu profiling
        start_batch = torch.cuda.Event(enable_timing=True)
        end_batch = torch.cuda.Event(enable_timing=True)
        start_run = torch.cuda.Event(enable_timing=True)
        end_run = torch.cuda.Event(enable_timing=True)

        start_batch.record()
        best_eval_acc, best_it = 0.0, 0

        scaler = GradScaler()
        amp_cm = autocast if args.amp else contextlib.nullcontext

        # eval for once to verify if the checkpoint is loaded correctly
        if args.resume == True:
            eval_dict = self.evaluate(args=args)
            print(eval_dict)

        selected_label = torch.ones((len(self.ulb_dset),), dtype=torch.long, ) * -1
        selected_label = selected_label.cuda(args.gpu)

        classwise_acc = torch.zeros((args.num_classes,)).cuda(args.gpu)

        for (_, x_lb, y_lb), (x_ulb_idx, x_ulb_w, x_ulb_s) in zip(self.loader_dict['train_lb'],
                                                                  self.loader_dict['train_ulb']):
            # prevent the training iterations exceed args.num_train_iter
            if self.it > args.num_train_iter:
                break

            end_batch.record()
            torch.cuda.synchronize()
            start_run.record()

            num_lb = x_lb.shape[0]
            num_ulb = x_ulb_w.shape[0]
            assert num_ulb == x_ulb_s.shape[0]

            x_lb, x_ulb_w, x_ulb_s = x_lb.cuda(args.gpu), x_ulb_w.cuda(args.gpu), x_ulb_s.cuda(args.gpu)
            x_ulb_idx = x_ulb_idx.cuda(args.gpu)
            y_lb = y_lb.cuda(args.gpu)

            pseudo_counter = Counter(selected_label.tolist())
            if max(pseudo_counter.values()) < len(self.ulb_dset):  # not all(5w) -1
                if args.thresh_warmup:
                    for i in range(args.num_classes):
                        classwise_acc[i] = pseudo_counter[i] / max(pseudo_counter.values())
                else:
                    wo_negative_one = deepcopy(pseudo_counter)
                    if -1 in wo_negative_one.keys():
                        wo_negative_one.pop(-1)
                    for i in range(args.num_classes):
                        classwise_acc[i] = pseudo_counter[i] / max(wo_negative_one.values())

            inputs = torch.cat((x_lb, x_ulb_w, x_ulb_s))

            # inference and calculate sup/unsup losses
            using_NCE=True
            num_split=1
            num_NCE_instance=20
            loss_NCE=0
            NCE_weight=0.02
            NCE_scale=4            
            with amp_cm():
                logits = self.model(inputs)
                logits_x_lb = logits[:num_lb]
                logits_x_ulb_w, logits_x_ulb_s = logits[num_lb:].chunk(2)
                #print(logits_x_ulb_w.shape)
                #exit()
                pseudo_label = torch.softmax(logits_x_ulb_w, dim=-1)
                max_probs, targets_u = torch.max(pseudo_label, dim=-1)
                class_id=targets_u.unique()
                selected_id=torch.tensor([i for i in range(logits_x_ulb_w.shape[0])])
                selected_id_list=[]
                for id in class_id:

                    mask_1_w_bjj = (targets_u==id).float().long()
      
                    mask_1_w_bjj=mask_1_w_bjj.reshape(-1)
                    index_b=torch.nonzero(mask_1_w_bjj)[0]
                    selected_id_list.append(index_b)
                    selected_id[index_b]=-1
                #print(   selected_id)
                #exit()
                mask_1_w_bjj = (selected_id>-1).float().long()    
                mask_1_w_bjj=mask_1_w_bjj.reshape(-1)
                index_b=torch.nonzero(mask_1_w_bjj)  
                print(index_b.shape)
                print(len(selected_id_list))
                #exit()
                id_list=list(index_b[:num_NCE_instance-len(selected_id_list)])+selected_id_list 
                print(id_list)
                if using_NCE:
                    w_soft_ori=F.softmax(logits_x_ulb_w,-1)
                    s_soft_ori=F.softmax(logits_x_ulb_s,-1) 
                    #print(w_soft_ori.shape,s_soft_ori.shape)
                    #exit()
                    for NCE_Loss_id in range(num_split):
                        w_soft=w_soft_ori[[1,88,7]]#[NCE_Loss_id*num_NCE_instance:(NCE_Loss_id+1)*num_NCE_instance]
                        s_soft=s_soft_ori[id_list]#[NCE_Loss_id*num_NCE_instance:(NCE_Loss_id+1)*num_NCE_instance]
                        #print(w_soft.shape)
                        #print(s_soft.shape)
                        #exit()
                        w_s_soft=torch.cat([w_soft,s_soft])
                        #print(w_s_soft.shape)
                        #exit()
                        targets_NCE=torch.from_numpy(np.array([x for x in range(num_NCE_instance)]))
                        out1_x=w_s_soft
                        NCE_2=torch.mm(out1_x,out1_x.transpose(0,1).contiguous())
                        unit_1=torch.eye(num_NCE_instance*2).cuda()

                        NCE_2=NCE_2*(1-unit_1)+(-100000)*unit_1
                        gt_labels_cls_cross=torch.cat([targets_NCE+num_NCE_instance,targets_NCE]).cuda()
                        loss_NCE = loss_NCE+NCE_weight*self.criterion(NCE_scale*NCE_2, gt_labels_cls_cross)                 
                sup_loss = ce_loss(logits_x_lb, y_lb, reduction='mean')+loss_NCE

                # hyper-params for update
                T = self.t_fn(self.it)
                p_cutoff = self.p_fn(self.it)

                unsup_loss, mask, select, pseudo_lb, p_model = consistency_loss(logits_x_ulb_s,
                                                                                logits_x_ulb_w,
                                                                                classwise_acc,
                                                                                p_target,
                                                                                p_model,
                                                                                'ce', T, p_cutoff,
                                                                                use_hard_labels=args.hard_label,
                                                                                use_DA=args.use_DA)

                if x_ulb_idx[select == 1].nelement() != 0:
                    selected_label[x_ulb_idx[select == 1]] = pseudo_lb[select == 1]

                total_loss = sup_loss + self.lambda_u * unsup_loss

            # parameter updates
            if args.amp:
                scaler.scale(total_loss).backward()
                if (args.clip > 0):
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), args.clip)
                scaler.step(self.optimizer)
                scaler.update()
            else:
                total_loss.backward()
                if (args.clip > 0):
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), args.clip)
                self.optimizer.step()

            self.scheduler.step()
            self.ema.update()
            self.model.zero_grad()

            end_run.record()
            torch.cuda.synchronize()

            # tensorboard_dict update
            tb_dict = {}
            tb_dict['train/sup_loss'] = sup_loss.detach()
            tb_dict['train/unsup_loss'] = unsup_loss.detach()
            tb_dict['train/total_loss'] = total_loss.detach()
            tb_dict['train/mask_ratio'] = 1.0 - mask.detach()
            tb_dict['lr'] = self.optimizer.param_groups[0]['lr']
            tb_dict['train/prefecth_time'] = start_batch.elapsed_time(end_batch) / 1000.
            tb_dict['train/run_time'] = start_run.elapsed_time(end_run) / 1000.

            # Save model for each 10K steps and best model for each 1K steps
            if self.it % 10000 == 0:
                save_path = os.path.join(args.save_dir, args.save_name)
                if not args.multiprocessing_distributed or \
                        (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
                    self.save_model('latest_model.pth', save_path)

            if self.it % self.num_eval_iter == 0:
                eval_dict = self.evaluate(args=args)
                tb_dict.update(eval_dict)
                save_path = os.path.join(args.save_dir, args.save_name)
                if tb_dict['eval/top-1-acc'] > best_eval_acc:
                    best_eval_acc = tb_dict['eval/top-1-acc']
                    best_it = self.it
                self.print_fn(
                    f"{self.it} iteration, USE_EMA: {self.ema_m != 0}, {tb_dict}, BEST_EVAL_ACC: {best_eval_acc}, at {best_it} iters")

                if not args.multiprocessing_distributed or \
                        (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):

                    if self.it == best_it:
                        self.save_model('model_best.pth', save_path)
                    if not self.tb_log is None:
                        self.tb_log.update(tb_dict, self.it)

            self.it += 1
            del tb_dict
            start_batch.record()
            if self.it > 0.8 * args.num_train_iter:
                self.num_eval_iter = 1000

        eval_dict = self.evaluate(args=args)
        eval_dict.update({'eval/best_acc': best_eval_acc, 'eval/best_it': best_it})
        return eval_dict

    @torch.no_grad()
    def evaluate(self, eval_loader=None, args=None):
        self.model.eval()
        self.ema.apply_shadow()
        if eval_loader is None:
            eval_loader = self.loader_dict['eval']
        total_loss = 0.0
        total_num = 0.0
        y_true = []
        y_pred = []
        y_logits = []
        for _, x, y in eval_loader:
            x, y = x.cuda(args.gpu), y.cuda(args.gpu)
            num_batch = x.shape[0]
            total_num += num_batch
            logits = self.model(x)
            loss = F.cross_entropy(logits, y, reduction='mean')
            y_true.extend(y.cpu().tolist())
            y_pred.extend(torch.max(logits, dim=-1)[1].cpu().tolist())
            y_logits.extend(torch.softmax(logits, dim=-1).cpu().tolist())
            total_loss += loss.detach() * num_batch
        top1 = accuracy_score(y_true, y_pred)
        top5 = top_k_accuracy_score(y_true, y_logits, k=5)
        precision = precision_score(y_true, y_pred, average='macro')
        recall = recall_score(y_true, y_pred, average='macro')
        F1 = f1_score(y_true, y_pred, average='macro')
        AUC = roc_auc_score(y_true, y_logits, multi_class='ovo')
        cf_mat = confusion_matrix(y_true, y_pred, normalize='true')
        self.print_fn('confusion matrix:\n' + np.array_str(cf_mat))
        self.ema.restore()
        self.model.train()
        return {'eval/loss': total_loss / total_num, 'eval/top-1-acc': top1, 'eval/top-5-acc': top5,
                'eval/precision': precision, 'eval/recall': recall, 'eval/F1': F1, 'eval/AUC': AUC}

    def save_model(self, save_name, save_path):
        save_filename = os.path.join(save_path, save_name)
        # copy EMA parameters to ema_model for saving with model as temp
        self.model.eval()
        self.ema.apply_shadow()
        ema_model = self.model.state_dict()
        self.ema.restore()
        self.model.train()

        torch.save({'model': self.model.state_dict(),
                    'optimizer': self.optimizer.state_dict(),
                    'scheduler': self.scheduler.state_dict(),
                    'it': self.it,
                    'ema_model': ema_model},
                   save_filename)

        self.print_fn(f"model saved: {save_filename}")

    def load_model(self, load_path):
        checkpoint = torch.load(load_path)

        self.model.load_state_dict(checkpoint['model'])
        self.ema_model = deepcopy(self.model)
        self.ema_model.load_state_dict(checkpoint['ema_model'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.scheduler.load_state_dict(checkpoint['scheduler'])
        self.it = checkpoint['it']
        self.print_fn('model loaded')

    def interleave_offsets(self, batch, nu):
        groups = [batch // (nu + 1)] * (nu + 1)
        for x in range(batch - sum(groups)):
            groups[-x - 1] += 1
        offsets = [0]
        for g in groups:
            offsets.append(offsets[-1] + g)
        assert offsets[-1] == batch
        return offsets

    def interleave(self, xy, batch):
        nu = len(xy) - 1
        offsets = self.interleave_offsets(batch, nu)
        xy = [[v[offsets[p]:offsets[p + 1]] for p in range(nu + 1)] for v in xy]
        for i in range(1, nu + 1):
            xy[0][i], xy[i][i] = xy[i][i], xy[0][i]
        return [torch.cat(v, dim=0) for v in xy]


if __name__ == "__main__":
    pass
