import logging

import mxnet as mx
from mxnet import gluon, nd, autograd
from mxnet.gluon import nn
import os
import numpy as np
import time

from mxnet_text_to_image.library.pool import ImagePool
from mxnet_text_to_image.utils.glove_loader import GloveModel
from mxnet_text_to_image.utils.image_utils import save_image, inverted_transform, Vgg16FeatureExtractor


def facc(label, pred):
    pred = pred.ravel()
    label = label.ravel()
    return ((pred > 0.5) == label).mean()


class Discriminator(nn.Block):

    def __init__(self, ndf):
        super(Discriminator, self).__init__()
        self.ndf = ndf
        with self.name_scope():
            netD = nn.Sequential()
            with netD.name_scope():
                # input shape: (?, num_channels, 64, 64)

                netD.add(nn.Conv2D(channels=ndf,
                                   kernel_size=4, strides=2,
                                   padding=1, use_bias=False))
                # netD.add(nn.BatchNorm())
                netD.add(nn.LeakyReLU(0.2))
                # state shape: (?, ndf, 32, 32)

                netD.add(nn.Conv2D(channels=ndf * 2,
                                   kernel_size=4, strides=2,
                                   padding=1, use_bias=False))
                netD.add(nn.BatchNorm())
                netD.add(nn.LeakyReLU(0.2))
                # state shape: (?, ndf * 2, 16, 16)

                netD.add(nn.Conv2D(channels=ndf * 4,
                                   kernel_size=4, strides=2,
                                   padding=1, use_bias=False))
                # netD.add(nn.BatchNorm())
                netD.add(nn.LeakyReLU(0.2))
                # state shape: (?, ndf * 4, 8, 8)

                netD.add(nn.Conv2D(channels=ndf * 8,
                                   kernel_size=4, strides=2,
                                   padding=1, use_bias=False))
                # netD.add(nn.BatchNorm())
                netD.add(nn.LeakyReLU(0.2))
                # state shape: (?, ndf * 8, 4, 4)

                netD.add(nn.Conv2D(channels=300,
                                   kernel_size=4, strides=1,
                                   padding=0, use_bias=False))
            self.netD = netD
            # self.fc1 = nn.Dense(1024, 'relu')
            # self.bn = nn.BatchNorm()
            self.dropout = nn.Dropout(.3)
            self.fc2 = nn.Dense(1)

    def forward(self, x, *args):
        x1 = self.netD(x[0])
        x1 = x1.reshape((x1.shape[0], 300))
        x2 = x[1]
        z = nd.concat(x1, x2, dim=1)
        # z = self.fc1(z)
        # z = self.bn(z)
        z = self.dropout(z)
        return self.fc2(z)


class DCGan(object):

    model_name = 'dcgan-v2'

    def __init__(self, model_ctx=mx.cpu(), data_ctx=mx.cpu()):
        self.netG = None
        self.netD = None
        self.model_ctx = model_ctx
        self.data_ctx = data_ctx
        self.random_input_size = 100
        self.glove = GloveModel()

    @staticmethod
    def get_config_file_path(model_dir_path):
        return os.path.join(model_dir_path, DCGan.model_name + '-config.npy')

    @staticmethod
    def get_params_file_path(model_dir_path, net_name):
        return os.path.join(model_dir_path, DCGan.model_name + '-' + net_name + '.params')

    def load_glove(self, glove_dir_path):
        self.glove.load(data_dir_path=glove_dir_path, embedding_dim=300)

    @staticmethod
    def create_model(num_channels=3, ngf=64, ndf=64):
        netG = nn.Sequential()
        with netG.name_scope():
            # input shape: (?, random_input_length + text_input_length, 1, 1)

            netG.add(nn.Conv2DTranspose(channels=ngf * 8,
                                        kernel_size=4, strides=1,
                                        padding=0, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 8, 4, 4)

            netG.add(nn.Conv2DTranspose(channels=ngf * 4,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 4, 8, 8)

            netG.add(nn.Conv2DTranspose(channels=ngf * 2,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 2, 16, 16)

            netG.add(nn.Conv2DTranspose(channels=ngf,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 2, 32, 32)

            netG.add(nn.Conv2DTranspose(channels=num_channels,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.Activation('tanh'))
            # state shape: (?, num_channels, 64, 64)

        netD = Discriminator(ndf)

        return netG, netD

    def load_model(self, model_dir_path):
        config = np.load(self.get_config_file_path(model_dir_path)).item()
        self.random_input_size = config['random_input_size']
        self.netG, self.netD = self.create_model()
        self.netG.load_params(self.get_params_file_path(model_dir_path, 'netG'), ctx=self.model_ctx)
        self.netD.load_params(self.get_params_file_path(model_dir_path, 'netD'), ctx=self.model_ctx)

    def checkpoint(self, model_dir_path):
        self.netG.save_params(self.get_params_file_path(model_dir_path, 'netG'))
        self.netD.save_params(self.get_params_file_path(model_dir_path, 'netD'))

    def fit(self, train_data, model_dir_path, image_dict, epochs=2, batch_size=64, learning_rate=0.0002, beta1=0.5,
            image_pool_size=50,
            start_epoch=0,
            print_every=10):

        config = dict()
        config['random_input_size'] = self.random_input_size
        np.save(self.get_config_file_path(model_dir_path), config)

        image_pool = ImagePool(image_pool_size)

        loss = gluon.loss.SigmoidBinaryCrossEntropyLoss()

        if self.netG is None:
            self.netG, self.netD = self.create_model()

            self.netG.initialize(mx.init.Normal(0.02), ctx=self.model_ctx)
            self.netD.initialize(mx.init.Normal(0.02), ctx=self.model_ctx)

        trainerG = gluon.Trainer(self.netG.collect_params(), 'adam', {'learning_rate': learning_rate, 'beta1': beta1})
        trainerD = gluon.Trainer(self.netD.collect_params(), 'adam', {'learning_rate': learning_rate, 'beta1': beta1})

        real_label = nd.ones((batch_size,), ctx=self.model_ctx)
        fake_label = nd.zeros((batch_size, ), ctx=self.model_ctx)

        metric = mx.metric.CustomMetric(facc)

        logging.basicConfig(level=logging.DEBUG)

        fake_images = []
        for epoch in range(start_epoch, epochs):
            tic = time.time()
            btic = time.time()
            train_data.reset()
            iter = 0
            for batch in train_data:

                # Step 1: Update netD
                real_images = list()
                real_image_ids = batch.data[0].as_in_context(self.model_ctx)
                for image_id in real_image_ids:
                    key = image_id.asscalar().astype(np.uint)
                    real_images.append(image_dict[key])
                real_images = nd.array(real_images, ctx=self.model_ctx)
                bsize = real_images.shape[0]
                text_feats = batch.data[1].as_in_context(self.model_ctx)
                random_input = nd.random_normal(0, 1, shape=(real_images.shape[0], self.random_input_size, 1, 1), ctx=self.model_ctx)

                fake_images = self.netG(nd.concat(random_input, text_feats.reshape((bsize, 300, 1, 1)), dim=1))
                fake_concat = image_pool.query([fake_images, text_feats])

                with autograd.record():
                    # train with real image
                    output = self.netD([real_images, text_feats])
                    errD_real = loss(output, real_label)
                    metric.update([real_label, ], [output, ])

                    # train with fake image
                    output = self.netD(fake_concat)
                    errD_fake = loss(output, fake_label)
                    errD = errD_real + errD_fake
                    errD.backward()
                    metric.update([fake_label, ], [output, ])

                trainerD.step(bsize)

                # Step 2: Update netG
                with autograd.record():
                    fake_images = self.netG(nd.concat(random_input, text_feats.reshape((bsize, 300, 1, 1)), dim=1))
                    output = self.netD([fake_images, text_feats])
                    errG = loss(output, real_label)
                    errG.backward()

                trainerG.step(bsize)

                # Print log infomation every ten batches
                if iter % print_every == 0:
                    name, acc = metric.get()
                    logging.info('speed: {} samples/s'.format(batch_size / (time.time() - btic)))
                    logging.info(
                        'discriminator loss = %f, generator loss = %f, binary training acc = %f at iter %d epoch %d'
                        % (nd.mean(errD).asscalar(),
                           nd.mean(errG).asscalar(), acc, iter, epoch))
                iter = iter + 1
                btic = time.time()

            name, acc = metric.get()
            metric.reset()
            logging.info('\nbinary training acc at epoch %d: %s=%f' % (epoch, name, acc))
            logging.info('time: %f' % (time.time() - tic))

            self.checkpoint(model_dir_path)

            # Visualize one generated image for each epoch
            fake_img = inverted_transform(fake_images[0]).asnumpy().astype(np.uint8)
            # fake_img = ((fake_img.asnumpy().transpose(1, 2, 0) + 1.0) * 127.5).astype(np.uint8)

            save_image(fake_img, os.path.join(model_dir_path, DCGan.model_name + '-training-') + str(epoch) + '.png')

    def generate(self, text_message, filename, output_dir_path):
        text_feats = self.glove.encode_doc(text_message)
        text_feats = nd.array(text_feats, ctx=self.model_ctx).reshape((1, 300, 1, 1))

        latent_z = nd.random_normal(loc=0, scale=1, shape=(1, self.random_input_size, 1, 1), ctx=self.model_ctx)
        img = self.netG(nd.concat(latent_z, text_feats, dim=1))[0]
        img = inverted_transform(img).asnumpy().astype(np.uint8)

        # img = ((img.asnumpy().transpose(1, 2, 0) + 1.0) * 127.5).astype(np.uint8)
        save_image(img, os.path.join(output_dir_path, filename))
        return img
