# -*- coding: utf-8 -*-

import logging

import numpy as np
import torch.nn as nn

from leibniz.nn.conv import (
    DepthwiseSeparableConv1d,
    DepthwiseSeparableConv2d,
    DepthwiseSeparableConv3d,
)
from leibniz.nn.layer.cbam import CBAM
from leibniz.nn.net.unet import Enconv, Deconv, Block
from leibniz.nn.net.hyptube import HypTube

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Transform(nn.Module):
    def __init__(
        self, in_channels, out_channels, nblks=10, block=None, relu=None, conv=nn.Conv2d
    ):

        super(Transform, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.nblks = nblks

        if relu is None:
            relu = nn.ReLU(inplace=True)

        self.relu = relu
        self.block = block(
            self.out_channels, step=1.0 / nblks, ix=0, tx=nblks, relu=relu, conv=conv
        )

    def forward(self, x):
        y = x
        for _ in range(self.nblks):
            self.block.ix = _ + 1
            y = self.relu(self.block(y))
        y = self.block(y)

        return y, x


class UNet3(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        block=None,
        attn=None,
        relu=None,
        layers=4,
        ratio=2,
        enhencer=None,
        ksize_in=7,
        dropout_prob=0.1,
        vblks=None,
        hblks=None,
        scales=None,
        factors=None,
        spatial=(256, 256),
        normalizor="batch",
        padding=None,
        final_normalized=True,
    ):
        super().__init__()

        extension = block.extension
        lrd = block.least_required_dim

        spatial = np.array(spatial, dtype=np.int32)
        dim = len(spatial)
        self.dim = dim
        Conv = self.get_conv_for_prepare()
        TConv = self.get_conv_for_transform()

        scales = np.array(scales)
        if scales.shape[0] != layers:
            raise ValueError("scales should have %d layers at dim 0!" % layers)
        if len(scales.shape) == 1:
            scales = scales.reshape([layers, 1])
        if len(scales.shape) != 2:
            raise ValueError(
                "scales should have length 2 to be compatible with spatial dimensions!"
            )

        ratio = np.exp2(ratio)
        factors = np.array(factors + [0.0])
        scales = np.exp2(scales)
        factors = np.exp2(factors)
        num_filters = int(in_channels * ratio)

        self.final_normalized = final_normalized
        self.ratio = ratio
        self.hblks = hblks
        self.vblks = vblks
        self.scales = scales
        self.factors = factors
        logger.info("---------------------------------------")
        logger.info("ratio: %f", ratio)
        logger.info("vblks: [%s]", ", ".join(map(str, vblks)))
        logger.info("hblks: [%s]", ", ".join(map(str, hblks)))
        logger.info("scales: [%s]", ", ".join(map(str, scales)))
        logger.info("factors: [%s]", ", ".join(map(str, factors[0:4])))
        logger.info("---------------------------------------")

        self.exceeded = np.any(np.cumprod(scales, axis=0) * spatial < 1) or np.any(
            (in_channels * ratio * np.cumprod(factors)) < lrd
        )
        if not self.exceeded:
            self.layers = layers
            self.in_channels = in_channels
            self.num_filters = num_filters
            self.out_channels = out_channels

            if enhencer is None:
                if self.dim == 2:
                    enhencer = HypTube
            self.enhencer_in = None
            self.enhencer_out = None
            self.enhencer_mid = None

            if relu is None:
                relu = nn.ReLU(inplace=True)

            if attn is None:
                attn = CBAM

            ex = extension
            c0 = int(ex * num_filters)
            if padding:
                self.conv_padding = 0
                self.iconv = nn.Sequential(
                    padding,
                    Conv(
                        in_channels,
                        c0,
                        kernel_size=ksize_in,
                        padding=(ksize_in - 1) // 2,
                        groups=1,
                    ),
                )
                self.oconv = nn.Sequential(
                    padding,
                    Conv(
                        c0,
                        out_channels,
                        kernel_size=3,
                        padding=self.conv_padding,
                        bias=False,
                        groups=1,
                    ),
                )
            else:
                self.conv_padding = 1
                self.iconv = Conv(
                    in_channels,
                    c0,
                    kernel_size=ksize_in,
                    padding=(ksize_in - 1) // 2,
                    groups=1,
                )
                self.oconv = Conv(
                    c0,
                    out_channels,
                    kernel_size=3,
                    padding=self.conv_padding,
                    bias=False,
                    groups=1,
                )

            if final_normalized:
                self.relu6 = nn.ReLU6()

            self.enconvs = nn.ModuleList()
            self.dnforms = nn.ModuleList()
            self.hzforms = nn.ModuleList()
            self.upforms = nn.ModuleList()
            self.deconvs = nn.ModuleList()

            self.spatial = [np.array(spatial, dtype=np.int32)]
            self.channel_sizes = [np.array(c0, dtype=np.int32)]
            for ix in range(layers):
                least_factor = ex
                scale, factor = scales[ix], factors[ix]
                self.spatial.append(np.array(self.spatial[ix] * scale, dtype=np.int32))
                self.channel_sizes.append(
                    np.array(
                        self.channel_sizes[ix] * factor // least_factor * least_factor,
                        dtype=np.int32,
                    )
                )

                ci, co = (
                    self.channel_sizes[ix].item(),
                    self.channel_sizes[ix + 1].item(),
                )
                szi, szo = self.spatial[ix + 1], self.spatial[ix]
                logger.info("%d - ci: %d, co: %d", ix, ci, co)
                logger.info(
                    "%d - szi: [%s], szo: [%s]",
                    ix,
                    ", ".join(map(str, szi)),
                    ", ".join(map(str, szo)),
                )

                self.exceeded = (
                    self.exceeded
                    or ci < lrd
                    or co < lrd
                    or szi.min() < 1
                    or szo.min() < 1
                )
                if not self.exceeded:
                    try:
                        dropout_flag = (layers - ix) * 3 < layers
                        dropout = dropout_prob if dropout_flag else -1
                        self.enconvs.append(
                            Block(
                                Enconv(ci, co, size=szi, conv=TConv, padding=padding),
                                activation=True,
                                dropout=dropout,
                                relu=relu,
                                attn=attn,
                                dim=self.dim,
                                normalizor=normalizor,
                                conv=TConv,
                            )
                        )
                        self.dnforms.append(
                            Transform(
                                co,
                                co,
                                nblks=vblks[ix],
                                block=block,
                                relu=relu,
                                conv=TConv,
                            )
                        )
                        self.hzforms.append(
                            Transform(
                                co,
                                co,
                                nblks=hblks[ix],
                                block=block,
                                relu=relu,
                                conv=TConv,
                            )
                        )
                        self.deconvs.append(
                            Block(
                                Deconv(
                                    co * 2, ci, size=szo, conv=TConv, padding=padding
                                ),
                                activation=True,
                                dropout=False,
                                relu=relu,
                                attn=attn,
                                dim=self.dim,
                                normalizor=normalizor,
                                conv=TConv,
                            )
                        )
                        self.upforms.append(
                            Transform(
                                ci,
                                ci,
                                nblks=vblks[ix],
                                block=block,
                                relu=relu,
                                conv=TConv,
                            )
                        )
                    except Exception as e:
                        logger.exception(e)
                        self.exceeded = True
                else:
                    logger.error("scales are exceeded!")
                    raise ValueError("scales exceeded!")

            if self.dim == 2 and enhencer is not None:
                self.enhencer_mid = enhencer(co, (c0 + 1) // 2, co)

    def get_conv_for_prepare(self):
        if self.dim == 1:
            conv = DepthwiseSeparableConv1d
        elif self.dim == 2:
            conv = DepthwiseSeparableConv2d
        elif self.dim == 3:
            conv = DepthwiseSeparableConv3d
        else:
            raise ValueError("dim %d is not supported!" % self.dim)
        return conv

    def get_conv_for_transform(self):
        if self.dim == 1:
            conv = DepthwiseSeparableConv1d
        elif self.dim == 2:
            conv = DepthwiseSeparableConv2d
        elif self.dim == 3:
            conv = DepthwiseSeparableConv3d
        else:
            raise ValueError("dim %d is not supported!" % self.dim)
        return conv

    def forward(self, x):
        if self.exceeded:
            raise ValueError("scales exceeded!")

        dnt = self.iconv(x)
        if self.enhencer_in is not None:
            dnt = self.enhencer_in(dnt)

        hzts = []
        for ix in range(self.layers):
            dnt, enc = self.dnforms[ix](self.enconvs[ix](dnt))
            hzt, _ = self.hzforms[ix](enc)
            hzts.append(hzt)

        if self.enhencer_mid is None:
            upt = dnt
        else:
            upt = self.enhencer_mid(dnt)

        for ix in range(self.layers - 1, -1, -1):
            hzt = hzts[ix]
            upt, dec = self.upforms[ix](self.deconvs[ix](upt, hzt))

        if self.enhencer_out is not None:
            upt = self.enhencer_out(upt)

        if self.final_normalized:
            return self.relu6(self.oconv(upt)) / 6
        else:
            return self.oconv(upt)
