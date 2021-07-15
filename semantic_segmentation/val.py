#!/usr/bin/python3
import time
import shutil
import random
import argparse
import numpy as np
from collections import deque

import paddle
import paddle.nn.functional as F
from config import *
from src.utils import  get_sys_env, metrics, TimeAverager, calculate_eta, logger, progbar
from src.core import infer
from src.datasets import get_dataset
from src.models import SETR
from src.utils import TimeAverager, calculate_eta, resume
from src.utils.utils import load_entire_model
from src.transforms import *




def parse_args():
    parser = argparse.ArgumentParser(description='Model Evaluation')
    parser.add_argument("--config", dest='cfg',help="The config file.", default=None, type=str)
    parser.add_argument('--model_path',dest='model_path', help='The path of weights file (segmentation model)',type=str,default=None)
    return parser.parse_args()


if __name__ == '__main__':
    config = get_config()
    args = parse_args()
    config = update_config(config, args)
    
    if args.model_path is None:
        args.model_path = os.path.join(config.SAVE_DIR,"iter_{}_model_state.pdparams".format(config.TRAIN.ITERS))

    env_info = get_sys_env()
    place = 'gpu' if env_info['Paddle compiled with cuda'] and env_info['GPUs used'] else 'cpu'
    paddle.set_device(place)
    # build model
    model = SETR(config)
    if args.model_path:
        load_entire_model(model, args.model_path)
        logger.info('Loaded trained params of model successfully')
    model.eval()

    nranks = paddle.distributed.ParallelEnv().nranks
    local_rank = paddle.distributed.ParallelEnv().local_rank
    if nranks > 1:
        # Initialize parallel environment if not done.
        if not paddle.distributed.parallel.parallel_helper._is_parallel_ctx_initialized():
            paddle.distributed.init_parallel_env()
            ddp_model = paddle.DataParallel(model)
        else:
            ddp_model = paddle.DataParallel(model)

    # build val dataset and dataloader
    transforms_val = [ Resize(target_size=config.VAL.IMAGE_BASE_SIZE),
                       Normalize(mean=[123.675, 116.28, 103.53], std=[58.395, 57.12, 57.375]) ]
    dataset_val = get_dataset(config, data_transform=transforms_val, mode='val')

    batch_sampler = paddle.io.DistributedBatchSampler(
        dataset_val, batch_size=config.DATA.BATCH_SIZE_VAL, shuffle=True, drop_last=True)
    loader_val = paddle.io.DataLoader( dataset_val, batch_sampler=batch_sampler,
        num_workers=config.DATA.NUM_WORKERS, return_list=True)

    total_iters = len(loader_val)

    # build workspace for saving checkpoints
    if not os.path.isdir(config.SAVE_DIR):
        if os.path.exists(config.SAVE_DIR):
            os.remove(config.SAVE_DIR)
        os.makedirs(config.SAVE_DIR)

    intersect_area_all = 0
    pred_area_all = 0
    label_area_all = 0

    logger.info("Start evaluating (total_samples: {}, total_iters: {})...".format(len(dataset_val), total_iters))
    progbar_val = progbar.Progbar(target=total_iters, verbose=1)
    reader_cost_averager = TimeAverager()
    batch_cost_averager = TimeAverager()
    batch_start = time.time()
    with paddle.no_grad():
        for iter, (im, label) in enumerate(loader_val):
            reader_cost_averager.record(time.time() - batch_start)
            label = label.astype('int64')
            print("img.shape: {}, label.shape: {}".format(im.shape, label.shape))
            ori_shape = label.shape[-2:]
            if config.VAL.MULTI_SCALES_VAL:
                pred = infer.aug_inference(
                    model,
                    im,
                    ori_shape=ori_shape,
                    transforms=transforms_val,
                    scales=scales,
                    flip_horizontal=flip_horizontal,
                    flip_vertical=flip_vertical,
                    is_slide=True,
                    stride=config.VAL.STRIDE,
                    crop_size=config.VAL.CROP_SIZE)
            else:
                pred = infer.inference(
                    model,
                    im,
                    ori_shape=ori_shape,
                    transforms=transforms_val,
                    is_slide=True,
                    stride=config.VAL.STRIDE_SIZE,
                    crop_size=config.VAL.CROP_SIZE,
                    num_classes=config.DATA.NUM_CLASSES)

            intersect_area, pred_area, label_area = metrics.calculate_area(
                pred,
                label,
                dataset_val.num_classes,
                ignore_index=dataset_val.ignore_index)

            # Gather from all ranks
            if nranks > 1:
                intersect_area_list = []
                pred_area_list = []
                label_area_list = []
                paddle.distributed.all_gather(intersect_area_list, intersect_area)
                paddle.distributed.all_gather(pred_area_list, pred_area)
                paddle.distributed.all_gather(label_area_list, label_area)

                # Some image has been evaluated and should be eliminated in last iter
                if (iter + 1) * nranks > len(dataset_val):
                    valid = len(dataset_val) - iter * nranks
                    intersect_area_list = intersect_area_list[:valid]
                    pred_area_list = pred_area_list[:valid]
                    label_area_list = label_area_list[:valid]

                for i in range(len(intersect_area_list)):
                    intersect_area_all = intersect_area_all + intersect_area_list[i]
                    pred_area_all = pred_area_all + pred_area_list[i]
                    label_area_all = label_area_all + label_area_list[i]
            else:
                intersect_area_all = intersect_area_all + intersect_area
                pred_area_all = pred_area_all + pred_area
                label_area_all = label_area_all + label_area
            batch_cost_averager.record(time.time() - batch_start, num_samples=len(label))
            batch_cost = batch_cost_averager.get_average()
            reader_cost = reader_cost_averager.get_average()

            if local_rank == 0 :
                progbar_val.update(iter + 1, [('batch_cost', batch_cost), ('reader cost', reader_cost)])
            reader_cost_averager.reset()
            batch_cost_averager.reset()
            batch_start = time.time()

    class_iou, miou = metrics.mean_iou(intersect_area_all, pred_area_all, label_area_all)
    class_acc, acc = metrics.accuracy(intersect_area_all, pred_area_all)
    kappa = metrics.kappa(intersect_area_all, pred_area_all, label_area_all)

    logger.info("[EVAL] #Images: {} mIoU: {:.4f} Acc: {:.4f} Kappa: {:.4f} ".format(len(dataset_val), miou, acc, kappa))
    logger.info("[EVAL] Class IoU: \n" + str(np.round(class_iou, 4)))
    logger.info("[EVAL] Class Acc: \n" + str(np.round(class_acc, 4)))