import os
import lmdb
import json
import pickle
import shutil
import argparse

import numpy as np
import pandas as pd
import tensorflow as tf

from numba import cuda
from pathlib import Path

from bcfind.config_manager import Configuration
from bcfind.models import UNet, SEUNet, ECAUNet, AttentionUNet, MoUNets
from bcfind.utils import sigmoid
from bcfind.localizers.blob_dog import BlobDoG
from bcfind.losses import FramedCrossentropy3D, FramedFocalCrossentropy3D
from bcfind.data import TrainingDataset, get_input_tf, normalize_tf


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        prog="train.py",
        formatter_class=lambda prog: argparse.ArgumentDefaultsHelpFormatter(
            prog, max_help_position=52, width=90
        ),
    )
    parser.add_argument("config", type=str, help="YAML Configuration file")
    return parser.parse_args()


def main():
    gpus = tf.config.list_physical_devices('GPU')
    tf.config.set_visible_devices(gpus[-1], 'GPU')
    tf.config.experimental.set_memory_growth(gpus[-1], True)

    args = parse_args()
    conf = Configuration(args.config)

    ######################################
    ############ CREATE DIRS #############
    ######################################
    config_name = args.config.split('/')[-1]
    if not os.path.isdir(conf.exp.basepath):
        os.makedirs(conf.exp.basepath)
        shutil.copyfile(args.config, f'{conf.exp.basepath}/{config_name}')
    
    if not os.path.isdir(conf.unet.checkpoint_dir):
        os.makedirs(conf.unet.checkpoint_dir)
    
    if not os.path.isdir(conf.dog.checkpoint_dir):
        os.makedirs(conf.dog.checkpoint_dir)
    
    if os.path.isdir(conf.unet.tensorboard_dir):
        shutil.rmtree(conf.unet.tensorboard_dir, ignore_errors=True)
        os.makedirs(conf.unet.tensorboard_dir)

    ####################################
    ############ UNET DATA #############
    ####################################
    print('\n LOADING UNET DATA')
    marker_list = [f'{conf.data.train_gt_dir}/{fname}' for fname in os.listdir(conf.data.train_gt_dir)]
    tiff_list = [f'{conf.data.train_tif_dir}/{fname}' for fname in os.listdir(conf.data.train_tif_dir)]

    ordered_tiff_list = []
    for f in marker_list:
        fname = Path(f).with_suffix('').name
        tiff_file = [f for f in map(lambda f: Path(f), tiff_list) if f.name == fname]
        ordered_tiff_list.append(str(tiff_file[0]))

    data = TrainingDataset(
        tiff_list=ordered_tiff_list, 
        marker_list=marker_list, 
        batch_size=conf.unet.batch_size, 
        dim_resolution=conf.data.dim_resolution, 
        output_shape=conf.unet.input_shape, 
        augmentations=conf.data_aug.op_args, 
        augmentations_prob=conf.data_aug.op_probs,
        )

    ####################################
    ############## UNET ################
    ####################################
    print()
    print('\n BUILDING UNET')
    if conf.unet.model == 'unet':
        unet = UNet(
            n_blocks=conf.unet.n_blocks, 
            n_filters=conf.unet.n_filters, 
            k_size=conf.unet.k_size, 
            k_stride=conf.unet.k_stride,
            dropout=conf.unet.dropout, 
            regularizer=conf.unet.regularizer
            )
    elif conf.unet.model == 'se-unet':
        unet = SEUNet(
            n_blocks=conf.unet.n_blocks, 
            n_filters=conf.unet.n_filters, 
            k_size=conf.unet.k_size, 
            k_stride=conf.unet.k_stride,
            squeeze_factor=conf.unet.squeeze_factor,
            dropout=conf.unet.dropout, 
            regularizer=conf.unet.regularizer
            )
    elif conf.unet.model == 'eca-unet':
        unet = ECAUNet(
            n_blocks=conf.unet.n_blocks, 
            n_filters=conf.unet.n_filters, 
            k_size=conf.unet.k_size, 
            k_stride=conf.unet.k_stride,
            dropout=conf.unet.dropout, 
            regularizer=conf.unet.regularizer
            )
    elif conf.unet.model == 'attention-unet':
        unet = AttentionUNet(
            n_blocks=conf.unet.n_blocks, 
            n_filters=conf.unet.n_filters, 
            k_size=conf.unet.k_size, 
            k_stride=conf.unet.k_stride,
            dropout=conf.unet.dropout, 
            regularizer=conf.unet.regularizer
            )
    elif conf.unet.model == 'moe-unet':
        unet = MoUNets(
            n_blocks=conf.unet.n_blocks, 
            n_filters=conf.unet.n_filters, 
            k_size=conf.unet.k_size, 
            k_stride=conf.unet.k_stride,
            n_experts=conf.unet.n_experts,
            keep_top_k=conf.unet.top_k_experts,
            add_noise=conf.unet.moe_noise,
            dropout=conf.unet.dropout, 
            regularizer=conf.unet.regularizer
            )
    else:
        raise ValueError(f'UNet model must be one of ["unet", "se-unet", "eca-unet", "attention-unet", "moe-unet"]. \
            Received {conf.unet.model}.')
    
    
    # loss = FramedFocalCrossentropy3D(exclude_border, input_shape, gamma=3, alpha=None, from_logits=True)
    loss = FramedCrossentropy3D(conf.unet.exclude_border, conf.unet.input_shape, from_logits=True)
    # loss = tf.keras.losses.BinaryCrossentropy(from_logits=True)
    lr_schedule = tf.keras.optimizers.schedules.CosineDecay(conf.unet.learning_rate, data.cardinality().numpy(), alpha=0.0)
    optimizer = tf.keras.optimizers.Adam(lr_schedule)

    unet.build((None, None, None, None, 1))
    unet.compile(loss=loss, optimizer=optimizer)
    unet.summary()

    MC_callback = tf.keras.callbacks.ModelCheckpoint(
        f"{conf.unet.checkpoint_dir}/model.tf",
        save_best_only=True,
        save_format='tf',
        save_freq="epoch",
        monitor="loss",
        mode="min",
        verbose=1,
    )

    TB_callback = tf.keras.callbacks.TensorBoard(
        conf.unet.tensorboard_dir,
        update_freq="epoch",
        profile_batch=0,
    )

    unet.fit(
        data,
        epochs=conf.unet.epochs,
        callbacks=[MC_callback, TB_callback],
        validation_data=None,
        verbose=1,
    )

    ####################################
    ############ LOAD UNET #############
    ####################################
    del unet

    # Load UNet and weights
    unet = tf.keras.models.load_model(f"{conf.unet.checkpoint_dir}/model.tf")
    unet.build((None, None, None, None, 1))
    
    ####################################
    ############ DOG DATA ##############
    ####################################
    print("\n LOADING DoG DATA")

    Y = []
    for marker_file in sorted(marker_list):
        print(f"Loading file {marker_file}")
        y = pd.read_csv(open(marker_file, "r"))
        y = y[conf.data.marker_columns].dropna(0)
        Y.append(np.array(y))

    print(f"Saving U-Net predictions in {conf.exp.basepath}/Train_pred_lmdb")

    n = len(marker_list)
    nbytes = np.prod(conf.data.shape) * 1 # 4 bytes for float32: 1 byte for uint8
    db = lmdb.open(f'{conf.exp.basepath}/Train_pred_lmdb', map_size=n*nbytes*10)

    with db.begin(write=True) as fx:
        for i, tiff_file in enumerate(ordered_tiff_list):
            print(f"Unet prediction on file {i+1}/{len(marker_list)}")
            
            x = get_input_tf(tf.constant(tiff_file))
            x = normalize_tf(x)        
            x = x[tf.newaxis, ..., tf.newaxis]

            max_attempts = 10
            attempt = 0
            while True:
                try:
                    print(x.shape)
                    pred = unet(x, training=False)
                    break
                except (tf.errors.InvalidArgumentError, ValueError) as e:
                    if attempt == max_attempts:
                        raise e
                    else:
                        if attempt % 2 == 0:
                            print('Invalid input shape for concat layer. Try padding axis 1')
                            paddings = tf.constant([[0, 0], [0, 1], [0, 0], [0, 0], [0, 0]])
                        else:
                            print('Invalid input shape for concat layer. Try padding axes [2, 3]')
                            paddings = tf.constant([[0, 0], [0, 0], [0, 1], [0, 1], [0, 0]])
                        
                        x = tf.pad(x, paddings)
                        attempt += 1

            pred = sigmoid(tf.squeeze(pred)) * 255

            pred = pred.astype('uint8')
            fname = tiff_file.split('/')[-1]
            fx.put(key=fname.encode(), value=pickle.dumps(pred))

    ####################################
    ############### DOG ################
    ####################################
    del unet
    cuda.close()

    dog = BlobDoG(3, conf.data.dim_resolution, conf.dog.exclude_border)
    with db.begin() as fx:
        X = fx.cursor()
        dog.fit(
            X=X,
            Y=Y,
            max_match_dist=conf.dog.max_match_dist,
            n_iter=conf.dog.iterations,
            checkpoint_dir=conf.dog.checkpoint_dir,
            n_cpu=conf.dog.n_cpu,
        )

    dog_par = dog.get_parameters()
    print(f"Best parameters found for DoG: {dog_par}")

    with open(f"{conf.dog.checkpoint_dir}/parameters.json", "w") as outfile:
        json.dump(dog_par, outfile)

    db.close()

if __name__ == '__main__':
    main()