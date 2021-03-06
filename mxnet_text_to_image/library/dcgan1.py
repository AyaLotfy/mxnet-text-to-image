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
            self.fc1 = nn.Dense(1024, 'relu')
            self.bn = nn.BatchNorm()
            self.dropout = nn.Dropout(.3)
            self.fc2 = nn.Dense(1)

    def forward(self, x, *args):
        x1 = x[0]
        x2 = x[1]
        z = nd.concat(x1, x2, dim=1)
        z = self.fc1(z)
        z = self.bn(z)
        z = self.dropout(z)
        return self.fc2(z)


class DCGan(object):

    model_name = 'dcgan-v1'

    def __init__(self, model_ctx=mx.cpu(), data_ctx=mx.cpu()):
        self.netG = None
        self.netD = None
        self.model_ctx = model_ctx
        self.data_ctx = data_ctx
        self.random_input_size = 100
        self.fe = Vgg16FeatureExtractor(model_ctx=model_ctx)
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
                                        kernel_size=7, strides=1,
                                        padding=0, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 8, 7, 7)

            netG.add(nn.Conv2DTranspose(channels=ngf * 4,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 4, 14, 14)

            netG.add(nn.Conv2DTranspose(channels=ngf * 2,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf * 2, 28, 28)

            netG.add(nn.Conv2DTranspose(channels=ngf,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf, 56, 56)

            netG.add(nn.Conv2DTranspose(channels=ngf // 2,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.BatchNorm())
            netG.add(nn.Activation('relu'))
            # state shape: (?, ngf, 112, 112)

            netG.add(nn.Conv2DTranspose(channels=num_channels,
                                        kernel_size=4, strides=2,
                                        padding=1, use_bias=False))
            netG.add(nn.Activation('tanh'))
            # state shape: (?, num_channels, 224, 224)

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

    def fit(self, train_data, image_feats_dict, model_dir_path, epochs=2, batch_size=64,
            image_pool_size=50,
            learning_rate=0.0002, beta1=0.5, print_every=2):

        config = dict()
        config['random_input_size'] = self.random_input_size
        np.save(self.get_config_file_path(model_dir_path), config)

        loss = gluon.loss.SigmoidBinaryCrossEntropyLoss()

        if self.netG is None:
            self.netG, self.netD = self.create_model()

            self.netG.initialize(mx.init.Normal(0.02), ctx=self.model_ctx)
            self.netD.initialize(mx.init.Normal(0.02), ctx=self.model_ctx)

        trainerG = gluon.Trainer(self.netG.collect_params(), 'adam', {'learning_rate': learning_rate, 'beta1': beta1})
        trainerD = gluon.Trainer(self.netD.collect_params(), 'adam', {'learning_rate': learning_rate, 'beta1': beta1})

        real_label = nd.ones((batch_size,), ctx=self.model_ctx)
        fake_label = nd.zeros((batch_size, ), ctx=self.model_ctx)

        image_pool = ImagePool(image_pool_size)

        metric = mx.metric.CustomMetric(facc)

        logging.basicConfig(level=logging.DEBUG)

        fake = []
        for epoch in range(epochs):
            tic = time.time()
            btic = time.time()
            train_data.reset()
            iter = 0
            for batch in train_data:

                # Step 1: Update netD
                real_image_ids = batch.data[0].as_in_context(self.model_ctx)
                real_image_feats = list()
                for image_id in real_image_ids:
                    real_image_feats.append(image_feats_dict[image_id.asscalar().astype(np.uint)])
                real_image_feats = nd.array(real_image_feats, ctx=self.model_ctx)
                bsize = real_image_feats.shape[0]
                text_feats = batch.data[1].as_in_context(self.model_ctx)
                random_input = nd.random_normal(0, 1, shape=(real_image_feats.shape[0], self.random_input_size, 1, 1), ctx=self.model_ctx)

                fake = self.netG(nd.concat(random_input, text_feats.reshape((bsize, 300, 1, 1)), dim=1))
                fake_feat = self.fe.image_net(fake)
                fake_concat = image_pool.query([real_image_feats, text_feats])

                with autograd.record():
                    # train with real image
                    output = self.netD(fake_concat)
                    errD_real = loss(output, real_label)
                    metric.update([real_label, ], [output, ])

                    # train with fake image
                    output = self.netD([fake_feat, text_feats])
                    errD_fake = loss(output, fake_label)
                    errD = errD_real + errD_fake
                    errD.backward()
                    metric.update([fake_label, ], [output, ])

                trainerD.step(batch.data[0].shape[0])

                # Step 2: Update netG
                with autograd.record():
                    fake = self.netG(nd.concat(random_input, text_feats.reshape((bsize, 300, 1, 1)), dim=1))
                    fake_feat = self.fe.image_net(fake)
                    output = self.netD([fake_feat, text_feats])
                    errG = loss(output, real_label)
                    errG.backward()

                trainerG.step(batch.data[0].shape[0])

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
            fake_img = inverted_transform(fake[0]).asnumpy().astype(np.uint8)
            # fake_img = ((fake_img.asnumpy().transpose(1, 2, 0) + 1.0) * 127.5).astype(np.uint8)

            save_image(fake_img, os.path.join(model_dir_path, DCGan.model_name + '-training-') + str(epoch) + '.png')

    def generate(self, text_message, num_images, output_dir_path):
        text_feats = self.glove.encode_doc(text_message)
        text_feats = nd.array(text_feats, ctx=self.model_ctx).reshape((1, 300, 1, 1))
        for i in range(num_images):
            latent_z = nd.random_normal(loc=0, scale=1, shape=(1, self.random_input_size, 1, 1), ctx=self.model_ctx)
            img = self.netG(nd.concat(latent_z, text_feats, dim=1))[0]
            img = inverted_transform(img).asnumpy().astype(np.uint8)
            # img = ((img.asnumpy().transpose(1, 2, 0) + 1.0) * 127.5).astype(np.uint8)
            save_image(img, os.path.join(output_dir_path, DCGan.model_name+'-generated-'+str(i) + '.png'))
