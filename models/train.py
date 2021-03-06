#!/usr/bin/env python
# -*- coding: utf-8 -*-
""" 
file: train.py
description: main training script for [arXiv/1705.02355]
author: Luke de Oliveira (lukedeo@manifold.ai), 
        Michela Paganini (michela.paganini@yale.edu)
"""

from __future__ import print_function

import argparse
from collections import defaultdict
import logging


import numpy as np
import os
import glob
from six.moves import range
from sklearn.preprocessing import LabelEncoder
from sklearn.utils import shuffle
import sys
import yaml
import pickle
import time


if __name__ == '__main__':
    logger = logging.getLogger(
        '%s.%s' % (
            __package__, os.path.splitext(os.path.split(__file__)[-1])[0]
        )
    )
    logger.setLevel(logging.INFO)
else:
    logger = logging.getLogger(__name__)


def bit_flip(x, prob=0.05):
    """ flips a int array's values with some probability """
    x = np.array(x)
    selection = np.random.uniform(0, 1, x.shape) < prob
    x[selection] = 1 * np.logical_not(x[selection])
    return x


def get_parser():
    parser = argparse.ArgumentParser(
        description='Run CalGAN training. '
        'Sensible defaults come from [arXiv/1511.06434]',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument('--nb-epochs', action='store', type=int, default=50,
                        help='Number of epochs to train for.')

    parser.add_argument('--batch-size', action='store', type=int, default=256,
                        help='batch size per update')

    parser.add_argument('--latent-size', action='store', type=int, default=1024,
                        help='size of random N(0, 1) latent space to sample')

    parser.add_argument('--disc-lr', action='store', type=float, default=2e-5,
                        help='Adam learning rate for discriminator')

    parser.add_argument('--gen-lr', action='store', type=float, default=2e-4,
                        help='Adam learning rate for generator')

    parser.add_argument('--adam-beta', action='store', type=float, default=0.5,
                        help='Adam beta_1 parameter')

    parser.add_argument('--weights-averaging-coeff', action='store', type=float, default=0.0,
                        help='Average loaded weights with a coefficient. 0: no averaging; 1: full (simple) averaging.')

    parser.add_argument('--prog-bar', action='store_true',
                        help='Whether or not to use a progress bar')

    parser.add_argument('--no-attn', action='store_true',
                        help='Whether to turn off the layer to layer attn.')

    parser.add_argument('--debug', action='store_true',
                        help='Whether to run debug level logging')

    parser.add_argument('--d-pfx', action='store',
                        default='params_discriminator_epoch_',
                        help='Default prefix for discriminator network weights')

    parser.add_argument('--g-pfx', action='store',
                        default='params_generator_epoch_',
                        help='Default prefix for generator network weights')

    parser.add_argument('--c-pfx', action='store',
                        default='params_combined_epoch_',
                        help='Default prefix for combined network weights')

    parser.add_argument('--load-model', action='store_true',
                         default=False, help='Load model from most recent .optimizer files')

    parser.add_argument('--save-model', action='store_true',
                         default=False, help='Save model into .optimizer files after each epoch')

    parser.add_argument('--load-weights', action='store_true',
                         default=False, help='Load weights from most recent .weights files')

    parser.add_argument('--process0', action='store_true',
                         default=False, help='Save and load weights and optimizer states only from process 0')

    parser.add_argument('--save-all-epochs', action='store_true',
                         default=False, help='Save weights and/or optimizer states from all epochs')

    parser.add_argument('--no-delete', action='store_true',
                         default=False, help='Do not delete weights and optimizer states after loading them')

    parser.add_argument('--last-activation', action='store', type=str, default='none',
                         help='Last activation function in the generator (none, softplus, leakyrelu)')

    parser.add_argument('--maintain-gen-loss-below', action='store', type=float, default=1000.0,
                         help='Maintain the generator loss below the specified value')

    parser.add_argument('--train-gen-per-epoch', action='store', type=int, default=1,
                         help='Train the generator n times per epoch')

    parser.add_argument('dataset', action='store', type=str,
                        help='yaml file with particles and HDF5 paths (see '
                        'github.com/hep-lbdl/CaloGAN/blob/master/models/'
                        'particles.yaml)')

    return parser


if __name__ == '__main__':

    parser = get_parser()
    parse_args = parser.parse_args()

    # delay the imports so running train.py -h doesn't take 5,234,807 years
    # from tf.compat.v1.keras import backend as K # TensofFlow 2.X
    import keras.backend as K # TensorFlow 1.X
    #import tensorflow.keras.backend as K
    # EV 10-Jan-2021 Import Horovod
    import horovod.keras as hvd
    import tensorflow as tf
    from keras.layers import (Activation, AveragePooling2D, Dense, Embedding,
                              Flatten, Input, Lambda, UpSampling2D)
    from keras.layers.merge import add, concatenate, multiply
    from keras.models import Model
    from keras.optimizers import Adam
    from keras.utils.generic_utils import Progbar
    from keras.callbacks import CallbackList
    from keras import models

    # EV 10-Jan-2021: initialize Horovod
    hvd.init()

    # EV 10-Jan-2021: Horovod: pin GPU to be used to process local rank (one GPU per process)
    try: 
        config = tf.ConfigProto() # TensorFlow 1.X
    except:
        config = tf.compat.v1.ConfigProto() # TensorFlow 2.X
    config.gpu_options.allow_growth = True
    config.gpu_options.visible_device_list = str(hvd.local_rank())
    K.set_session(tf.Session(config=config))

    K.common.set_image_dim_ordering('tf')

    from models.ops import (minibatch_discriminator, minibatch_output_shape, Dense3D,
                     calculate_energy, scale, inpainting_attention)

    from models.architectures import build_generator, build_discriminator

    # batch, latent size, and whether or not to be verbose with a progress bar

    if parse_args.debug:
        logger.setLevel(logging.DEBUG)

    # set up all the logging stuff
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s'
        '[%(levelname)s]: %(message)s'
    )
    hander = logging.StreamHandler(sys.stdout)
    hander.setFormatter(formatter)
    logger.addHandler(hander)

    nb_epochs = parse_args.nb_epochs
    #nb_epochs = int(parse_args.nb_epochs / hvd.size())

    batch_size = parse_args.batch_size
    latent_size = parse_args.latent_size
    verbose = parse_args.prog_bar
    no_attn = parse_args.no_attn

    load_model = parse_args.load_model
    save_model = parse_args.save_model
    load_weights = parse_args.load_weights
    process0 = parse_args.process0
    no_delete = parse_args.no_delete
    last_activation = parse_args.last_activation
    maintain_gen_loss_below = parse_args.maintain_gen_loss_below
    train_gen_per_epoch = parse_args.train_gen_per_epoch
    save_all_epochs = parse_args.save_all_epochs
    weights_averaging_coeff = parse_args.weights_averaging_coeff

    # EV 10-Jan-2021 Adjust the learning rate
    #disc_lr = parse_args.disc_lr
    #gen_lr = parse_args.gen_lr
    disc_lr = parse_args.disc_lr * 1
    gen_lr = parse_args.gen_lr * 1

    adam_beta_1 = parse_args.adam_beta

    yaml_file = parse_args.dataset

    logger.debug('hvd.size() = {}'.format(hvd.size()))

    print('hvd.size() = {}'.format(hvd.size()))
    print('hvd.local_size() = {}'.format(hvd.local_size()))
    print('hvd.rank() = {}'.format(hvd.rank()))
    print('hvd.local_rank() = {}'.format(hvd.local_rank()))
    print('load_model =',load_model)

    logger.debug('parameter configuration:')

    logger.debug('number of epochs = {}'.format(nb_epochs))
    logger.debug('batch size = {}'.format(batch_size))
    logger.debug('latent size = {}'.format(latent_size))
    logger.debug('progress bar enabled = {}'.format(verbose))
    logger.debug('Using attention = {}'.format(no_attn == False))
    logger.debug('discriminator learning rate = {}'.format(disc_lr))
    logger.debug('generator learning rate = {}'.format(gen_lr))
    logger.debug('Adam $\beta_1$ parameter = {}'.format(adam_beta_1))
    logger.debug('Will read YAML spec from {}'.format(yaml_file))

    # read in data file spec from YAML file
    with open(yaml_file, 'r') as stream:
        try:
            s = yaml.load(stream)
        except yaml.YAMLError as exc:
            logger.error(exc)
            raise exc
    nb_classes = len(s.keys())
    logger.info('{} particle types found.'.format(nb_classes))
    if sys.version_info[0] < 3: # Python 2.X
        for name, pth in s.iteritems():
            logger.debug('class {} <= {}'.format(name, pth))
    else: # Python 3.X
        for name, pth in s.items():
            logger.debug('class {} <= {}'.format(name, pth))

    def _load_data(particle, datafile):

        import h5py

        d = h5py.File(datafile, 'r')

        # make our calo images channels-last
        first = np.expand_dims(d['layer_0'][:], -1)
        second = np.expand_dims(d['layer_1'][:], -1)
        third = np.expand_dims(d['layer_2'][:], -1)
        # convert to MeV
        energy = d['energy'][:].reshape(-1, 1) * 1000

        sizes = [
            first.shape[1], first.shape[2],
            second.shape[1], second.shape[2],
            third.shape[1], third.shape[2]
        ]

        y = [particle] * first.shape[0]

        d.close()

        return first, second, third, y, energy, sizes

    logger.debug('loading data from {} files'.format(nb_classes))

    if sys.version_info[0] < 3: # Python 2.X
        first, second, third, y, energy, sizes = [
            np.concatenate(t) for t in [
                a for a in zip(*[_load_data(p, f) for p, f in s.iteritems()])
            ]
        ]
    else: # Python 3.X
        first, second, third, y, energy, sizes = [
            np.concatenate(t) for t in [
                a for a in zip(*[_load_data(p, f) for p, f in s.items()])
            ]
        ]

    # TO-DO: check that all sizes match, so I could be taking any of them
    sizes = sizes[:6].tolist()

    # scale the energy depositions by 1000 to convert MeV => GeV
    first, second, third, energy = [
        (X.astype(np.float32) / 1000)
        for X in [first, second, third, energy]
    ]

    le = LabelEncoder()
    y = le.fit_transform(y)

    first, second, third, y, energy = shuffle(first, second, third, y, energy,
                                              random_state=0)

    logger.info('Building discriminator')

    calorimeter = [Input(shape=sizes[:2] + [1]),
                   Input(shape=sizes[2:4] + [1]),
                   Input(shape=sizes[4:] + [1])]

    input_energy = Input(shape=(1, ))

    features = []
    energies = []

    for l in range(3):
        # build features per layer of calorimeter
        features.append(build_discriminator(
            image=calorimeter[l],
            mbd=True,
            sparsity=True,
            sparsity_mbd=True
        ))

        energies.append(calculate_energy(calorimeter[l]))

    features = concatenate(features)

    # This is a (None, 3) tensor with the individual energy per layer
    energies = concatenate(energies)

    # calculate the total energy across all rows
    total_energy = Lambda(
        lambda x: K.reshape(K.sum(x, axis=-1), (-1, 1)),
        name='total_energy'
    )(energies)

    # construct MBD on the raw energies
    nb_features = 10
    vspace_dim = 10
    minibatch_featurizer = Lambda(minibatch_discriminator,
                                  output_shape=minibatch_output_shape)
    K_energy = Dense3D(nb_features, vspace_dim)(energies)

    # constrain w/ a tanh to dampen the unbounded nature of energy-space
    mbd_energy = Activation('tanh')(minibatch_featurizer(K_energy))

    # absolute deviation away from input energy. Technically we can learn
    # this, but since we want to get as close as possible to conservation of
    # energy, just coding it in is better
    energy_well = Lambda(
        lambda x: K.abs(x[0] - x[1])
    )([total_energy, input_energy])

    # binary y/n if it is over the input energy
    well_too_big = Lambda(lambda x: 10 * K.cast(x > 5, K.floatx()))(energy_well)

    p = concatenate([
        features,
        scale(energies, 10),
        scale(total_energy, 100),
        energy_well,
        well_too_big,
        mbd_energy
    ])

    fake = Dense(1, activation='sigmoid', name='fakereal_output')(p)
    discriminator_outputs = [fake, total_energy]
    discriminator_losses = ['binary_crossentropy', 'mae']
    # ACGAN case
    if nb_classes > 1:
        logger.info('running in ACGAN for discriminator mode since found {} '
                    'classes'.format(nb_classes))

        aux = Dense(1, activation='sigmoid', name='auxiliary_output')(p)
        discriminator_outputs.append(aux)

        # change the loss depending on how many outputs on the auxiliary task
        if nb_classes > 2:
            discriminator_losses.append('sparse_categorical_crossentropy')
        else:
            discriminator_losses.append('binary_crossentropy')

    discriminator = Model(calorimeter + [input_energy], discriminator_outputs)

    discriminator.compile(
        # EV 10-Jan-2021: add Horovod Distributed Optimizer
        #optimizer=Adam(lr=disc_lr, beta_1=adam_beta_1),
        optimizer=hvd.DistributedOptimizer(Adam(lr=disc_lr, beta_1=adam_beta_1)),
        loss=discriminator_losses
    )

    logger.info('Building generator')

    latent = Input(shape=(latent_size, ), name='z')
    input_energy = Input(shape=(1, ), dtype='float32')
    generator_inputs = [latent, input_energy]

    # ACGAN case
    if nb_classes > 1:
        logger.info('running in ACGAN for generator mode since found {} '
                    'classes'.format(nb_classes))

        # label of requested class
        image_class = Input(shape=(1, ), dtype='int32')
        lookup_table = Embedding(nb_classes, latent_size, input_length=1,
                                 embeddings_initializer='glorot_normal')
        emb = Flatten()(lookup_table(image_class))

        # hadamard product between z-space and a class conditional embedding
        hc = multiply([latent, emb])

        # requested energy comes in GeV
        h = Lambda(lambda x: x[0] * x[1])([hc, scale(input_energy, 100)])
        generator_inputs.append(image_class)
    else:
        # requested energy comes in GeV
        h = Lambda(lambda x: x[0] * x[1])([latent, scale(input_energy, 100)])

    # each of these builds a LAGAN-inspired [arXiv/1701.05927] component with
    # linear last layer
    img_layer0 = build_generator(h, 3, 96, last_activation=last_activation)
    img_layer1 = build_generator(h, 12, 12, last_activation=last_activation)
    img_layer2 = build_generator(h, 12, 6, last_activation=last_activation)

    if not no_attn:

        logger.info('using attentional mechanism')

        # resizes from (3, 96) => (12, 12)
        zero2one = AveragePooling2D(pool_size=(1, 8))(
            UpSampling2D(size=(4, 1))(img_layer0))
        img_layer1 = inpainting_attention(img_layer1, zero2one)

        # resizes from (12, 12) => (12, 6)
        one2two = AveragePooling2D(pool_size=(1, 2))(img_layer1)
        img_layer2 = inpainting_attention(img_layer2, one2two)

    generator_outputs = [
        Activation('relu')(img_layer0),
        Activation('relu')(img_layer1),
        Activation('relu')(img_layer2)
    ]

    generator = Model(generator_inputs, generator_outputs)

    discriminator.trainable = False
    
    generator.compile(
        # EV 10-Jan-2021: add Horovod Distributed Optimizer
        #optimizer=Adam(lr=gen_lr, beta_1=adam_beta_1),
        optimizer=hvd.DistributedOptimizer(Adam(lr=gen_lr, beta_1=adam_beta_1)),
        loss='binary_crossentropy'
    )


    combined_outputs = discriminator(
        generator(generator_inputs) + [input_energy]
    )

    combined = Model(generator_inputs, combined_outputs, name='combined_model')
    combined.compile(
        # EV 10-Jan-2021: add Horovod Distributed Optimizer
        #optimizer=Adam(lr=gen_lr, beta_1=adam_beta_1),
        optimizer=hvd.DistributedOptimizer(Adam(lr=gen_lr, beta_1=adam_beta_1)),
        loss=discriminator_losses
    )

    last_epoch_gen_loss = None
    last_epoch_disc_loss = None

    def train_gan(epoch, nb_batches):

        if verbose:
            progress_bar = Progbar(target=nb_batches)

        epoch_gen_loss = []
        epoch_disc_loss = []

        for index in range(nb_batches):
            if verbose:
                progress_bar.update(index)
            else:
                if index % 100 == 0:
                    logger.info('processed {}/{} batches'.format(index + 1, nb_batches))
                elif index % 10 == 0:
                    logger.debug('processed {}/{} batches'.format(index + 1, nb_batches))

            # generate a new batch of noise
            noise = np.random.normal(0, 1, (batch_size, latent_size))

            # get a batch of real images
            image_batch_1 = first[index * batch_size:(index + 1) * batch_size]
            image_batch_2 = second[index * batch_size:(index + 1) * batch_size]
            image_batch_3 = third[index * batch_size:(index + 1) * batch_size]
            label_batch = y[index * batch_size:(index + 1) * batch_size]
            energy_batch = energy[index * batch_size:(index + 1) * batch_size]

            # energy_breakdown

            sampled_labels = np.random.randint(0, nb_classes, batch_size)
            sampled_energies = np.random.uniform(1, 100, (batch_size, 1))

            generator_inputs = [noise, sampled_energies]
            if nb_classes > 1:
                # in the case of the ACGAN, we need to append the requested
                # class to the pre-image of the generator
                generator_inputs.append(sampled_labels)

            generated_images = generator.predict(generator_inputs, verbose=0)

            disc_outputs_real = [np.ones(batch_size), energy_batch]
            disc_outputs_fake = [np.zeros(batch_size), sampled_energies]

            # downweight the energy reconstruction loss ($\lambda_E$ in paper)
            loss_weights = [np.ones(batch_size), 0.05 * np.ones(batch_size)]
            if nb_classes > 1:
                # in the case of the ACGAN, we need to append the realrequested
                # class to the target
                disc_outputs_real.append(label_batch)
                disc_outputs_fake.append(bit_flip(sampled_labels, 0.3))
                loss_weights.append(0.2 * np.ones(batch_size))

            if (last_epoch_gen_loss is None) or (last_epoch_gen_loss < maintain_gen_loss_below) or (epoch < 10):

              real_batch_loss = discriminator.train_on_batch(
                  [image_batch_1, image_batch_2, image_batch_3, energy_batch],
                  disc_outputs_real,
                  loss_weights
              )

              # note that a given batch should have either *only* real or *only* fake,
              # as we have both minibatch discrimination and batch normalization, both
              # of which rely on batch level stats
              fake_batch_loss = discriminator.train_on_batch(
                  generated_images + [sampled_energies],
                  disc_outputs_fake,
                  loss_weights
              )

            epoch_disc_loss.append(
                (np.array(fake_batch_loss) + np.array(real_batch_loss)) / 2)

            # we want to train the genrator to trick the discriminator
            # For the generator, we want all the {fake, real} labels to say
            # real
            trick = np.ones(batch_size)

            gen_losses = []

            # we do this twice simply to match the number of batches per epoch used to
            # train the discriminator
            for _ in range(2*train_gen_per_epoch):
                noise = np.random.normal(0, 1, (batch_size, latent_size))

                sampled_energies = np.random.uniform(1, 100, (batch_size, 1))
                combined_inputs = [noise, sampled_energies]
                combined_outputs = [trick, sampled_energies]
                if nb_classes > 1:
                    sampled_labels = np.random.randint(0, nb_classes,
                                                       batch_size)
                    combined_inputs.append(sampled_labels)
                    combined_outputs.append(sampled_labels)

                gen_losses.append(combined.train_on_batch(
                    combined_inputs,
                    combined_outputs,
                    loss_weights
                ))

            epoch_gen_loss.append(np.mean(np.array(gen_losses), axis=0))

        logger.info('Epoch {:3d} Generator loss: {}'.format(
            epoch + 1, np.mean(epoch_gen_loss, axis=0)))
        logger.info('Epoch {:3d} Discriminator loss: {}'.format(
            epoch + 1, np.mean(epoch_disc_loss, axis=0)))
        last_epoch_disc_loss = np.mean(epoch_disc_loss, axis=0)[0]
        last_epoch_gen_lss = np.mean(epoch_gen_loss, axis=0)[0]

    last_epoch = -1
    rank_to_load = 0 if process0 else hvd.rank()

    # EV 07-Mar-2021: Load weights and optimizer states if load_model=True

    def getLastEpoch(filenamemask):
        files = glob.glob(filenamemask)
        if len(files)==0:
            return -1
        latest_file = max(files, key=os.path.getctime)
        newstr = ''.join((ch if ch in '0123456789' else ' ') for ch in latest_file)
        listOfNumbers = [float(i) for i in newstr.split()]
        last_epoch = int(listOfNumbers[0])
        print("The last epoch was {}".format(last_epoch))
        return last_epoch


    if load_model:
        # Get latest epoch for saved optimizer state data
        last_epoch = getLastEpoch('{0}*_{1:03d}.optimizer'.format(parse_args.c_pfx,rank_to_load))
        print("Latest epoch in optimizer state data: {}".format(last_epoch))


    if load_model and (last_epoch>-1):
        # Run training for one dummy epoch to initialize gradients
        train_gan(0,1)

        # Load generator optimizer state
        filename = '{0}{1:04d}_{2:03d}.optimizer'.format(parse_args.g_pfx,last_epoch,rank_to_load)
        files = glob.glob(filename)
        if len(files)==0:
            raise Exception("Generator optimizer state file {} not found".format(filename))
        print("Using generator optimizer state from {}".format(filename))
        opt_weights = np.load(filename, allow_pickle=True)
        generator.optimizer.set_weights(opt_weights)
        
        # Load discriminator optimizer state
        filename = '{0}{1:04d}_{2:03d}.optimizer'.format(parse_args.d_pfx,last_epoch,rank_to_load)
        files = glob.glob(filename)
        if len(files)==0:
            raise Exception("Discriminator optimizer state file {} not found".format(filename))
        print("Using discriminator optimizer state from {}".format(filename))
        opt_weights = np.load(filename, allow_pickle=True)
        discriminator.optimizer.set_weights(opt_weights)

        # Load combined optimizer state
        filename = '{0}{1:04d}_{2:03d}.optimizer'.format(parse_args.c_pfx,last_epoch,rank_to_load)
        files = glob.glob(filename)
        if len(files)==0:
            raise Exception("Combined optimizer state file {} not found".format(filename))
        print("Using combined optimizer state from {}".format(filename))
        opt_weights = np.load(filename, allow_pickle=True)
        combined.optimizer.set_weights(opt_weights)
        if not no_delete:
            print("Sleeping 120 seconds...")
            time.sleep(120)
            os.system("rm -rf *.optimizer")


    if load_weights and not(load_model):
        last_epoch = getLastEpoch('{0}*_{1:03d}.weights'.format(parse_args.d_pfx,rank_to_load))
        print("Latest epoch in weights data: {}".format(last_epoch))


    if (load_weights or load_model) and (last_epoch>-1):
        if not load_model:
            last_epoch = getLastEpoch('{0}*.weights'.format(parse_args.d_pfx))
            print("Latest epoch in weights data: {}".format(last_epoch))
        
        # Load generator weights
        filename = '{0}{1:04d}_{2:03d}.weights'.format(parse_args.g_pfx,last_epoch,rank_to_load)
        files = glob.glob(filename)
        if len(files)==0:
            raise Exception("Generator weights file {} not found".format(filename))
        #latest_file = max(files, key=os.path.getctime)
        print("Using generator weights from {}".format(filename))
        generator.load_weights(filename)

        # Load discriminator weights
        filename = '{0}{1:04d}_{2:03d}.weights'.format(parse_args.d_pfx,last_epoch,rank_to_load)
        files = glob.glob(filename)
        if len(files)==0:
            raise Exception("Generator weights file {} not found".format(filename))
        #latest_file = max(files, key=os.path.getctime)
        print("Using discriminator weights from {}".format(filename))
        discriminator.load_weights(filename)

        if weights_averaging_coeff!=0.0:
            this_generator_weights = generator.get_weights()
            all_generator_weights = []
            for rank in range(0,hvd.size()):
                filename = '{0}{1:04d}_{2:03d}.weights'.format(parse_args.g_pfx,last_epoch,rank)
                files = glob.glob(filename)
                if len(files)==0:
                    raise Exception("Generator weights file {} not found".format(filename))
                generator.load_weights(filename)
                all_generator_weights.append(generator.get_weights())
            new_weights = list()
            for weights_list_tuple in zip(*all_generator_weights):
                new_weights.append([np.array(weights_).mean(axis=0) for weights_ in zip(*weights_list_tuple)])
            weights_to_load = list()
            for weights_list_tuple in zip(*[new_weights, this_generator_weights]):
                weights_to_load.append([weights_averaging_coeff*np.array(weights_[0])+(1.0-weights_averaging_coeff)*np.array(weights_[1]) for weights_ in zip(*weights_list_tuple)])
            generator.set_weights(weights_to_load)

            this_discriminator_weights = discriminator.get_weights()
            all_discriminator_weights = []
            for rank in range(0,hvd.size()):
                filename = '{0}{1:04d}_{2:03d}.weights'.format(parse_args.d_pfx,last_epoch,rank)
                files = glob.glob(filename)
                if len(files)==0:
                    raise Exception("Generator weights file {} not found".format(filename))
                discriminator.load_weights(filename)
                all_discriminator_weights.append(discriminator.get_weights())
            new_weights = list()
            for weights_list_tuple in zip(*all_discriminator_weights):
                new_weights.append([np.array(weights_).mean(axis=0) for weights_ in zip(*weights_list_tuple)])
            weights_to_load = list()
            for weights_list_tuple in zip(*[new_weights, this_discriminator_weights]):
                weights_to_load.append([weights_averaging_coeff*np.array(weights_[0])+(1.0-weights_averaging_coeff)*np.array(weights_[1]) for weights_ in zip(*weights_list_tuple)])
            discriminator.set_weights(weights_to_load)

        if not no_delete:
            print("Sleeping 120 seconds...")
            time.sleep(120)
            os.system("rm -rf *.weights")


    # EV 10-Jan-2021: Broadcast initial variable states from rank 0 to all other processes
    # EV 06-Fev-2021: add hvd.callbacks.MetricAverageCallback()
    
    gcb = CallbackList([hvd.callbacks.BroadcastGlobalVariablesCallback(0), hvd.callbacks.MetricAverageCallback()])
    dcb = CallbackList([hvd.callbacks.BroadcastGlobalVariablesCallback(0), hvd.callbacks.MetricAverageCallback()])
    ccb = CallbackList([hvd.callbacks.BroadcastGlobalVariablesCallback(0), hvd.callbacks.MetricAverageCallback()])

    gcb.set_model( generator )
    dcb.set_model( discriminator )
    ccb.set_model( combined )


    gcb.on_train_begin()
    dcb.on_train_begin()
    ccb.on_train_begin()

    logger.info('commencing training')

    for epoch in range(last_epoch+1, nb_epochs+last_epoch+1):

        logger.info('Epoch {} of {}'.format(epoch + 1, nb_epochs+last_epoch+1))
        nb_batches = int(first.shape[0] / batch_size)
        
        train_gan(epoch, nb_batches)

        # save weights every epoch
        # EV 10-Jan-2021: this needs to done only on one process. Otherwise each worker is writing it.
        if ((hvd.rank()==0) or (not process0)) and (save_all_epochs or epoch==nb_epochs+last_epoch):
            generator.save_weights('{0}{1:04d}_{2:03d}.weights'.format(parse_args.g_pfx, epoch, hvd.rank()),
                               overwrite=True)

            discriminator.save_weights('{0}{1:04d}_{2:03d}.weights'.format(parse_args.d_pfx, epoch, hvd.rank()),
                                   overwrite=True)
            if save_model:
                #generator.save('generator{0:04d}.model'.format(epoch),
                #               overwrite=True)
                #discriminator.save('discriminator{0:04d}.model'.format(epoch),
                #               overwrite=True)
                #combined.save('combined{0:04d}.model'.format(epoch),
                #               overwrite=True)
                # Save optimizer state for generator model
                with open('{0}{1:04d}_{2:03d}.optimizer'.format(parse_args.g_pfx, epoch, hvd.rank()), 'wb') as f:
                    np.save(f, generator.optimizer.get_weights())
                # Save optimizer state for discriminator model
                with open('{0}{1:04d}_{2:03d}.optimizer'.format(parse_args.d_pfx, epoch, hvd.rank()), 'wb') as f:
                    np.save(f, discriminator.optimizer.get_weights())
                # Save optimizer state for the combined model
                with open('{0}{1:04d}_{2:03d}.optimizer'.format(parse_args.c_pfx, epoch, hvd.rank()), 'wb') as f:
                    np.save(f, combined.optimizer.get_weights())
