# Copyright 2016 Stanford University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import random

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from tensorflow.python.framework import ops
from tensorflow.python.framework import dtypes
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import rnn
from tensorflow.python.ops import rnn_cell
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops.math_ops import sigmoid
from tensorflow.python.ops.math_ops import tanh


class GRUCellAttn(rnn_cell.GRUCell):
  def __init__(self, num_units, encoder_output, scope=None):
    self.hs = encoder_output
    with vs.variable_scope(scope or type(self).__name__):
      with vs.variable_scope("Attn1"):
        hs2d = tf.reshape(self.hs, [-1, num_units])
        phi_hs2d = tanh(rnn_cell.linear(hs2d, num_units, True, 1.0))
        self.phi_hs = tf.reshape(phi_hs2d, tf.shape(self.hs))
    super(GRUCellAttn, self).__init__(num_units)

  def __call__(self, inputs, state, scope=None):
    gru_out, gru_state = super(GRUCellAttn, self).__call__(inputs, state, scope)
    with vs.variable_scope(scope or type(self).__name__):
      with vs.variable_scope("Attn2"):
        gamma_h = tanh(rnn_cell.linear(gru_out, self._num_units, True, 1.0))
      weights = tf.reduce_sum(self.phi_hs * gamma_h, reduction_indices=2, keep_dims=True)
      weights = tf.exp(weights - tf.reduce_max(weights, reduction_indices=0, keep_dims=True))
      weights = weights / (1e-6 + tf.reduce_sum(weights, reduction_indices=0, keep_dims=True))
      context = tf.reduce_sum(self.hs * weights, reduction_indices=0)
      with vs.variable_scope("AttnConcat"):
        out = tf.nn.relu(rnn_cell.linear([context, gru_out], self._num_units, True, 1.0))
      attn_map = tf.reduce_sum(tf.slice(weights, [0, 0, 0], [-1, -1, 1]), reduction_indices=2)
      return (out, out) 

class NLCModel(object):
  def __init__(self, vocab_size, size, num_layers, max_gradient_norm, batch_size, learning_rate,
               learning_rate_decay_factor, dropout, forward_only=False):

    self.size = size
    self.vocab_size = vocab_size
    self.batch_size = batch_size
    self.num_layers = num_layers
    self.keep_prob = 1.0 - dropout
    self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
    self.learning_rate_decay_op = self.learning_rate.assign(self.learning_rate * learning_rate_decay_factor)
    self.global_step = tf.Variable(0, trainable=False)

    self.source_tokens = tf.placeholder(tf.int32, shape=[None, self.batch_size], name="source_tokens")
    self.target_tokens = tf.placeholder(tf.int32, shape=[None, self.batch_size], name="target_tokens")
    self.source_mask = tf.placeholder(tf.int32, shape=[None, self.batch_size], name="source_mask")
    self.target_mask = tf.placeholder(tf.int32, shape=[None, self.batch_size], name="target_mask")
    self.source_length = tf.reduce_sum(self.source_mask, reduction_indices=0)
    self.target_length = tf.reduce_sum(self.target_mask, reduction_indices=0)

    self.setup_embeddings()
    self.setup_encoder()
    self.setup_decoder()
    self.setup_loss()

    params = tf.trainable_variables()
    if not forward_only:
      opt = tf.train.AdamOptimizer(self.learning_rate)

      gradients = tf.gradients(self.losses, params)
      clipped_gradients, _ = tf.clip_by_global_norm(gradients, max_gradient_norm)
      self.gradient_norm = tf.global_norm(clipped_gradients)
      self.param_norm = tf.global_norm(params)
      self.updates = opt.apply_gradients(
        zip(clipped_gradients, params), global_step=self.global_step)

    self.saver = tf.train.Saver(tf.all_variables())

  def setup_embeddings(self):
    with vs.variable_scope("embeddings"):
      self.L_enc = tf.get_variable("L_enc", [self.vocab_size, self.size])
      self.L_dec = tf.get_variable("L_dec", [self.vocab_size, self.size])
      self.encoder_inputs = embedding_ops.embedding_lookup(self.L_enc, self.source_tokens)
      self.decoder_inputs = embedding_ops.embedding_lookup(self.L_dec, self.target_tokens)

  def setup_encoder(self):
    self.encoder_cell = rnn_cell.GRUCell(self.size)
    with vs.variable_scope("PryamidEncoder"):
      inp = self.encoder_inputs
      mask = self.source_mask
      out = None
      for i in xrange(self.num_layers):
        with vs.variable_scope("EncoderCell%d" % i) as scope:
          srclen = tf.reduce_sum(mask, reduction_indices=0)
          out, _ = self.bidirectional_rnn(self.encoder_cell, self.dropout(inp), srclen, scope=scope)
          inp, mask = self.downscale(out, mask)
      self.encoder_output = out

  def setup_decoder(self):
    if self.num_layers > 1:
      self.decoder_cell = rnn_cell.GRUCell(self.size)
    self.attn_cell = GRUCellAttn(self.size, self.encoder_output, scope="DecoderAttnCell")

    out = self.decoder_inputs

    with vs.variable_scope("Decoder"):
      inp = self.decoder_inputs
      for i in xrange(self.num_layers - 1):
        with vs.variable_scope("DecoderCell%d" % i) as scope:
          out, _ = rnn.dynamic_rnn(self.decoder_cell, self.dropout(inp), time_major=True,
                                   dtype=dtypes.float32, sequence_length=self.target_length,
                                   scope=scope)
          inp = out
      with vs.variable_scope("DecoderAttnCell") as scope:
        out, _ = rnn.dynamic_rnn(self.attn_cell, self.dropout(inp), time_major=True,
                                 dtype=dtypes.float32, sequence_length=self.target_length,
                                 scope=scope)
        self.decoder_output = out

  def setup_loss(self):
    with vs.variable_scope("Logistic"):
      do2d = tf.reshape(self.decoder_output, [-1, self.size])
      logits2d = rnn_cell.linear(do2d, self.vocab_size, True, 1.0)
      outputs2d = tf.nn.softmax(logits2d)
      self.outputs = tf.reshape(outputs2d, [-1, self.batch_size, self.vocab_size])

      targets_no_GO = tf.slice(self.target_tokens, [1, 0], [-1, -1])
      masks_no_GO = tf.slice(self.target_mask, [1, 0], [-1, -1])
      # easier to pad target/mask than to split decoder input since tensorflow does not support negative indexing
      labels1d = tf.reshape(tf.pad(targets_no_GO, [[0, 1], [0, 0]]), [-1])
      mask1d = tf.reshape(tf.pad(masks_no_GO, [[0, 1], [0, 0]]), [-1])
      losses1d = tf.nn.sparse_softmax_cross_entropy_with_logits(logits2d, labels1d) * tf.to_float(mask1d)
      losses2d = tf.reshape(losses1d, [-1, self.batch_size])
      self.losses = tf.reduce_sum(losses2d) / self.batch_size

  def dropout(self, inp):
    return tf.nn.dropout(inp, self.keep_prob)

  def downscale(self, inp, mask):
    with vs.variable_scope("Downscale"):
      inp2d = tf.reshape(tf.transpose(inp, perm=[1, 0, 2]), [-1, 2 * self.size])
      out2d = rnn_cell.linear(inp2d, self.size, True, 1.0)
      out3d = tf.reshape(out2d, [self.batch_size, -1, self.size])
      out3d = tf.transpose(out3d, perm=[1, 0, 2])
      out = tanh(out3d)

      mask = tf.transpose(mask)
      mask = tf.reshape(mask, [-1, 2])
      mask = tf.cast(mask, tf.bool)
      mask = tf.reduce_any(mask, reduction_indices=1)
      mask = tf.to_int32(mask)
      mask = tf.reshape(mask, [self.batch_size, -1])
      mask = tf.transpose(mask)
    return out, mask

  def bidirectional_rnn(self, cell, inputs, lengths, scope=None):
    name = scope.name or "BiRNN"
    # Forward direction
    with vs.variable_scope(name + "_FW") as fw_scope:
      output_fw, output_state_fw = rnn.dynamic_rnn(cell, inputs, time_major=True, dtype=dtypes.float32,
                                                   sequence_length=lengths, scope=fw_scope)
    # Backward direction
    with vs.variable_scope(name + "_BW") as bw_scope:
      output_bw, output_state_bw = rnn.dynamic_rnn(cell, inputs, time_major=True, dtype=dtypes.float32,
                                                   sequence_length=lengths, scope=bw_scope)

    output_bw = tf.reverse_sequence(output_bw, tf.to_int64(lengths), seq_dim=0, batch_dim=1)

    outputs = output_fw + output_bw
    output_state = output_state_fw + output_state_bw

    return (outputs, output_state)

  def train(self, session, source_tokens, source_mask, target_tokens, target_mask):
    input_feed = {}
    input_feed[self.source_tokens] = source_tokens
    input_feed[self.target_tokens] = target_tokens
    input_feed[self.source_mask] = source_mask
    input_feed[self.target_mask] = target_mask

    output_feed = [self.updates, self.gradient_norm, self.losses, self.param_norm]

    outputs = session.run(output_feed, input_feed)

    return outputs[1], outputs[2], outputs[3]

  def test(self, session, source_tokens, source_mask, target_tokens, target_mask):
    input_feed = {}
    input_feed[self.source_tokens] = source_tokens
    input_feed[self.target_tokens] = target_tokens
    input_feed[self.source_mask] = source_mask
    input_feed[self.target_mask] = target_mask

    output_feed = [self.losses, self.outputs]

    outputs = session.run(output_feed, input_feed)

    return outputs[0], outputs[1]
