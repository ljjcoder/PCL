# 
# ----------------------------------------------
import torch
import torch.nn.functional as F
from mmcv.runner.hooks import Hook
from clsda.utils.metrics import runningMetric
from clsda.utils import get_root_logger, get_root_writer
from mmcv.runner import get_dist_info
import pickle


class ClsAccuracy(Hook):
    def __init__(self, runner, dataset_name, major_comparison=False, pred_key='pred'):
        rank, _ = get_dist_info()
        self.local_rank = rank
        self.dataset_name = dataset_name
        if rank == 0:
            self.running_metrics = runningMetric()  #
            log_interval = max(len(runner.test_loaders[dataset_name]) - 1, 1)
            self.running_metrics.add_metrics('{}_cls'.format(pred_key), group_name='val_loss', metric_type='avgmeter',
                                             log_interval=log_interval)
        self.major_comparison = major_comparison
        self.best_acc = 0.0
        self.current_acc = 0.0
        self.pred_key = pred_key

    def before_val_epoch(self, runner):
        num_class = runner.trainer.num_class
        self.confusion_metric = torch.zeros((num_class, num_class), device="cuda:{}".format(self.local_rank))

    def after_val_iter(self, runner):
        batch_output = runner.batch_output
        dataset_name = batch_output['dataset_name']
        if dataset_name == self.dataset_name:
            gt = batch_output['gt']
            pred = batch_output[self.pred_key]
            pred_max = torch.argmax(pred, dim=1)
            for i in range(gt.shape[0]):
                self.confusion_metric[gt[i], pred_max[i]] += 1
            #
            loss = F.cross_entropy(pred, gt)
            if self.local_rank == 0:
                batch_metrics = {'val_loss': {'{}_cls'.format(self.pred_key): loss.item()}}
                self.running_metrics.update_metrics(batch_metrics)

    def after_val_epoch(self, runner):
        #
        tmp_confusion_mat = self.confusion_metric
        torch.distributed.reduce(tmp_confusion_mat, dst=0, op=torch.distributed.ReduceOp.SUM)
        # torch.distributed.barrier()
        #
        correct_count = torch.sum(torch.diag(tmp_confusion_mat)).item()
        total_count = torch.sum(tmp_confusion_mat).item()
        if self.local_rank == 0:
            acc = correct_count / total_count
            self.current_acc = acc
            if acc > self.best_acc:
                self.best_acc = acc
                if self.major_comparison:
                    runner.save_flag = True
            #
            logger = get_root_logger()
            writer = get_root_writer()
            #
            self.running_metrics.log_metrics(runner.iteration, force_log=True)
            # 总的准确率
            writer.add_scalar('{}_acc_{}'.format(self.pred_key, self.dataset_name), acc,
                              global_step=runner.iteration)
            class_acc = []
            class_sum = torch.sum(tmp_confusion_mat, dim=1)
            # 类别平均的准确率
            for i in range(runner.trainer.num_class):
                tmp_acc = tmp_confusion_mat[i, i].item() / class_sum[i].item()
                class_acc.append(tmp_acc)
                writer.add_scalar('{}_class_wise_acc_{}/class_{}'.format(self.pred_key, self.dataset_name, i), tmp_acc,
                                  global_step=runner.iteration)
            writer.add_scalar('{}_class_wise_acc_{}/0_mean'.format(self.pred_key, self.dataset_name),
                              sum(class_acc) / runner.trainer.num_class, global_step=runner.iteration)
            logger.info('Iteration {}: {} {} acc {}'.format(runner.iteration, self.pred_key, self.dataset_name, acc))
            logger.info(
                'total img of {} is {}, right {}'.format(self.dataset_name, total_count, correct_count))
        #
        self.confusion_metric = None
