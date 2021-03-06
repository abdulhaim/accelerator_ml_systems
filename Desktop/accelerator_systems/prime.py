from absl import app
from absl import flags
from absl import logging

# These tensorflow installs are automatically provided by the
# Google colab runtime. If you want to run this code locally,
# make sure to install tensorflow and tensorflow_probability.
import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp
import numpy as np
import os
import pickle
import csv
from typing import Optional, Dict, List
from copy import deepcopy

gfile = tf.io.gfile.GFile

# Default area constraint for the models we train
AREA_THRESHOLD = 27.0

# @title Basic utility functions for training transformers
"""
Code largely taken from https://www.tensorflow.org/text/tutorials/transformer
"""


def get_angles(pos, i, d_model):
    """Get angles for using tansformer."""
    angle_rates = 1 / np.power(10000, (2 * (i // 2)) / np.float32(d_model))
    return pos * angle_rates


def positional_encoding(position, d_model):
    """Obtain positional encdoing for training the PRIME Transformer."""
    angle_rads = get_angles(np.arange(position)[:, np.newaxis],
                            np.arange(d_model)[np.newaxis, :],
                            d_model)

    # apply sin to even indices in the array; 2i
    angle_rads[:, 0::2] = np.sin(angle_rads[:, 0::2])

    # apply cos to odd indices in the array; 2i+1
    angle_rads[:, 1::2] = np.cos(angle_rads[:, 1::2])

    pos_encoding = angle_rads[np.newaxis, ...]

    return tf.cast(pos_encoding, dtype=tf.float32)


class SplitEmbeddingLayer(tf.keras.layers.Layer):
    """Layer for embedding individual components in a split way"""

    def __init__(self, softmax_splits=None, output_size=32):
        """
    Initialize the layer to split the input and generate embeddings for
    each field.
    """
        super(SplitEmbeddingLayer, self).__init__(trainable=True)
        self.softmax_splits = softmax_splits
        self.output_size = output_size

        # create layers
        self.dense_layers = []
        # print(self.softmax_splits)
        for idx, val in enumerate(self.softmax_splits):
            self.dense_layers.append(
                tf.keras.layers.Dense(
                    self.output_size, name='insidelayer_' + str(idx)))

        # Add position embeddings
        self.pos_encoding = positional_encoding(position=200, d_model=output_size)

    def call(self, x):
        """Call the Split embedding function."""
        split_x = tf.split(x, num_or_size_splits=self.softmax_splits, axis=-1)
        modified_splits = []
        idx = 0
        for param in split_x:
            out = self.dense_layers[int(idx)](param)
            modified_splits.append(tf.expand_dims(out, axis=1))
            idx += 1
        out = tf.concat(modified_splits, axis=1)
        # print ('Out shape before: ', out)
        out = out + self.pos_encoding[:, :len(modified_splits), :]
        # print ('Out shape after: ', out)
        return out


def scaled_dot_product_attention(q, k, v, mask):
    """Scaled dot product attention in transformer."""
    matmul_qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)

    # scale matmul_qk
    dk = tf.cast(tf.shape(k)[-1], tf.float32)
    scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)

    # add the mask to the scaled tensor.
    if mask is not None:
        scaled_attention_logits += (mask * -1e9)

    # softmax is normalized on the last axis (seq_len_k) so that the scores
    # add up to 1.
    attention_weights = tf.nn.softmax(
        scaled_attention_logits, axis=-1)  # (..., seq_len_q, seq_len_k)
    output = tf.matmul(attention_weights, v)  # (..., seq_len_q, depth_v)
    return output, attention_weights


def point_wise_feed_forward_network(d_model, dff):
    return tf.keras.Sequential([
        tf.keras.layers.Dense(dff, activation='relu'),
        tf.keras.layers.Dense(d_model)  # (batch_size, seq_len, d_model)
    ])


class MultiHeadAttention(tf.keras.layers.Layer):
    """Multi Head Attention for the model."""

    def __init__(self, d_model, num_heads):
        """Initialize the multi-head attention model."""
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads
        self.d_model = d_model

        assert d_model % self.num_heads == 0

        self.depth = d_model // self.num_heads

        self.wq = tf.keras.layers.Dense(d_model)
        self.wk = tf.keras.layers.Dense(d_model)
        self.wv = tf.keras.layers.Dense(d_model)

        self.dense = tf.keras.layers.Dense(d_model)

    def split_heads(self, x, batch_size):
        """Split the last dimension into (num_heads, depth).
    Transpose the result such that the
    shape is (batch_size, num_heads, seq_len, depth)
    """
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, v, k, q, mask):
        batch_size = tf.shape(q)[0]

        q = self.wq(q)
        k = self.wk(k)
        v = self.wv(v)

        q = self.split_heads(q, batch_size)
        k = self.split_heads(k, batch_size)
        v = self.split_heads(v, batch_size)

        scaled_attention, attention_weights = scaled_dot_product_attention(
            q, k, v, mask)

        scaled_attention = tf.transpose(scaled_attention, perm=[0, 2, 1, 3])

        concat_attention = tf.reshape(scaled_attention,
                                      (batch_size, -1, self.d_model))

        output = self.dense(concat_attention)

        return output, attention_weights


class TransformerLayer(tf.keras.layers.Layer):
    """Define the transformer layer to be used in the PRIME Transformer model."""

    def __init__(self, d_model, num_heads, dff, rate=0.1):
        """Initialize the transformer layer."""
        super(TransformerLayer, self).__init__()

        self.mha = MultiHeadAttention(d_model, num_heads)
        self.ffn = point_wise_feed_forward_network(d_model, dff)

        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)

    def call(self, x, training=True, mask=None):
        attn_output, _ = self.mha(x, x, x, mask)
        # (batch_size, input_seq_len, d_model)
        attn_output = self.dropout1(attn_output, training=training)
        out1 = self.layernorm1(x + attn_output)
        # (batch_size, input_seq_len, d_model)

        ffn_output = self.ffn(out1)  # (batch_size, input_seq_len, d_model)
        ffn_output = self.dropout2(ffn_output, training=training)
        out2 = self.layernorm2(out1 + ffn_output)
        # (batch_size, input_seq_len, d_model)
        return out2

#@title Helper functions for MSE/Huber Loss computation

def weighted_mse_loss(input, target, weight):
  """Compute weighted MSE Loss"""
  mse_loss_val = (tf.squeeze(input) - tf.squeeze(target))**2
  return tf.reduce_mean(mse_loss_val * tf.squeeze(weight))


def weighted_huber_loss(input, target, weight):
  """Compute weighted Huber Loss"""
  mse_loss = tf.keras.losses.Huber(
      reduction=tf.keras.losses.Reduction.NONE)
  return tf.reduce_mean(mse_loss(
      y_pred=tf.squeeze(input),
      y_true=tf.squeeze(target)) * tf.squeeze(weight))


def weighted_approx_loss(input, target, weight):
  """Compute weighted Approximation Loss"""
  abs_diff = tf.abs(tf.squeeze(input) - tf.squeeze(target))
  ratio_diff = abs_diff / (tf.abs(tf.squeeze(target)) + 1e-6)
  return tf.reduce_mean(ratio_diff * tf.squeeze(weight))

#@title Helper functions for ranking loss computation

def ranking_loss(input, target, context=None):
  """Compute measures of ranking for the PRIMETransformerModel."""
  if context is not None:
    # Compute ranking loss per context, and then average it.
    unique_contexts, indices = tf.unique(
        tf.squeeze(tf.cast(context, tf.int32)), name='None')
    all_corr = []
    for idx in range(unique_contexts.shape[0]):
      curr_context = unique_contexts[idx]
      locations_idx = tf.squeeze(tf.where(tf.equal(indices, curr_context)))
      input_tmp = tf.gather(
          tf.squeeze(input), indices=locations_idx)
      target_tmp = tf.gather(
          tf.squeeze(target), indices=locations_idx)
      input_ranks = tf.argsort(input_tmp, axis=-1)
      target_ranks = tf.argsort(target_tmp, axis=-1)
      input_ranks = tf.cast(tf.argsort(input_ranks, axis=-1), dtype=tf.float32)
      target_ranks = tf.cast(tf.argsort(target_ranks, axis=-1),
                             dtype=tf.float32)
      std_input = tf.math.reduce_std(input_ranks)
      std_target = tf.math.reduce_std(target_ranks)
      cov = tf.reduce_mean((target_ranks - tf.reduce_mean(target_ranks)) *\
                           (input_ranks - tf.reduce_mean(input_ranks)))
      pearson_corr = cov/ (std_target * std_input)
      all_corr.append(pearson_corr)
    print (all_corr)
    pearson_corr = tf.reduce_mean(pearson_corr)
  else:
    input = tf.squeeze(input)
    target = tf.squeeze(target)
    input_ranks = tf.argsort(input, axis=-1)
    target_ranks = tf.argsort(target, axis=-1)
    input_ranks = tf.cast(tf.argsort(input_ranks, axis=-1), dtype=tf.float32)
    target_ranks = tf.cast(tf.argsort(target_ranks, axis=-1), dtype=tf.float32)
    std_input = tf.math.reduce_std(input_ranks)
    std_target = tf.math.reduce_std(target_ranks)
    cov = tf.reduce_mean((target_ranks - tf.reduce_mean(target_ranks)) *\
                         (input_ranks - tf.reduce_mean(input_ranks)))
    pearson_corr = cov/ (std_target * std_input)
  return pearson_corr


def ranking_trainable_loss(input, target, context=None):
  """Compute a differentiable ranking loss, that can be used for training."""
  if context is not None:
    unique_contexts, indices = tf.unique(
        tf.squeeze(tf.cast(context, tf.int32)), name='None')
    all_corr = []
    for idx in range(unique_contexts.shape[0]):
      curr_context = unique_contexts[idx]
      locations_idx = tf.squeeze(tf.where(tf.equal(indices, curr_context)))
      input_tmp = tf.expand_dims(tf.gather(
          tf.squeeze(input), indices=locations_idx), 1)
      target_tmp = tf.expand_dims(tf.gather(
          tf.squeeze(target), indices=locations_idx), 1)
      input_transpose = tf.transpose(input_tmp, [1, 0]) # 1 x B
      target_transpose = tf.transpose(target_tmp, [1, 0]) # 1 x B
      diff_true = input_tmp - input_transpose # B x 1 - 1 x B = B x B = y_i - y_j
      diff_pred = target_tmp - target_transpose # fx_i - fx_j
      product = tf.sign(diff_true) * diff_pred  # sign(y_i = y_j) * (fx_i - fxj)
      bce_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
          labels=tf.ones_like(product), logits=product))
      all_corr.append(bce_loss)
    bce_loss = tf.reduce_mean(all_corr)
  else:
    input_transpose = tf.transpose(input, [1, 0]) # 1 x B
    target_transpose = tf.transpose(target, [1, 0]) # 1 x B
    diff_true = input - input_transpose # B x 1 - 1 x B = B x B = y_i - y_j
    diff_pred = target - target_transpose # fx_i - fx_j
    product = tf.sign(diff_true) * diff_pred  # sign(y_i = y_j) * (fx_i - fxj)
    bce_loss = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.ones_like(product), logits=product)
  return tf.reduce_mean(bce_loss)


#@title Helper function for Kendall correlation

def kendall_correlation(input, target, context=None):
  """Compute Kendall's correlation over the input, target and context."""
  if context is not None:
    unique_contexts, indices = tf.unique(
        tf.squeeze(tf.cast(context, tf.int32)), name='None')
    all_corr = []
    for idx in range(unique_contexts.shape[0]):
      curr_context = unique_contexts[idx]
      locations_idx = tf.squeeze(tf.where(tf.equal(indices, curr_context)))
      input_tmp = tf.expand_dims(tf.gather(
          tf.squeeze(input), indices=locations_idx), 1)
      target_tmp = tf.expand_dims(tf.gather(
          tf.squeeze(target), indices=locations_idx), 1)
      input_transpose = tf.transpose(input_tmp, [1, 0])
      target_transpose = tf.transpose(target_tmp, [1, 0])
      diff_true = input_tmp - input_transpose
      diff_pred = target_tmp - target_transpose
      product = tf.sign(diff_true) * tf.sign(diff_pred)
      positive_pairs = tf.where(tf.greater_equal(product, tf.zeros_like(product)),
                                tf.ones_like(product), tf.zeros_like(product))
      n = tf.cast(tf.shape(input_tmp)[0], dtype=tf.float32)
      total_positive = tf.reduce_sum(positive_pairs) - n
      ratio = total_positive/ (n * (n-1))
      all_corr.append(ratio)
    ratio = tf.reduce_mean(all_corr)
  else:
    input_transpose = tf.transpose(input, [1, 0])
    target_transpose = tf.transpose(target, [1, 0])
    diff_true = input - input_transpose
    diff_pred = target - target_transpose
    product = tf.sign(diff_true) * tf.sign(diff_pred)
    positive_pairs = tf.where(tf.greater_equal(product, tf.zeros_like(product)),
                              tf.ones_like(product), tf.zeros_like(product))
    n = tf.cast(tf.shape(input)[0], dtype=tf.float32)
    total_positive = tf.reduce_sum(positive_pairs) - n
    ratio = total_positive/ (n * (n-1))
  return 2 * ratio - 1.0


# @title Definition of the PRIME surrogate model, training procedure

class PRIMETransformerModel(tf.keras.Model):
  """
  The transformer model used by PRIME. This class implements ability to
  instantiate a transformer model, and train it via the PRIME training objective
  (Equation 3 in https://arxiv.org/abs/2110.11346).

  Additionally it also implements the ability to train a contextual model,
  conditioned on the context.
  """

  def __init__(self,
               num_outputs,
               num_inputs,
               optimizer,
               layers=(256, 256, 256),
               penalty_weight=10.0,
               negative_sampler=None,
               contextual=False,
               params_dict=None):
    """Initializes the PRIMETransformer model.

    Args:
      num_outputs: the dimensionality of the output of the PRIME surrogate.
        Typically set to 1, but you can increase it to model multiple cost
        functions together.
      num_inputs: the dimensionality of the total number of inputs to the model.
      optimizer: the optimizer to optimize the trainable model.
      layers: hidden layer sizes for the feed-forward layers after extracting
        the transformer embedding.
      penalty_weight: the value of alpha in Equation 2 in PRIME.
      negative_sampler: an instance of a negative sampler. A negative sampler
        is basically an optimizer that can take in the current snapshot of the
        this PRIMETransformerModel, and optimize the predictions of the current
        model snapshot w.r.t its input. In the paper, we utilize an evolutionary
        optimizer to optimize the predictions. For this code release, we present
        a simple gradient-descent based optimizer for optimization as a
        demonstration. Users are encouraged to pass in their relevant
        negative sampler here.
      contextual: bool, indicates whether we are training a contextual model
        or a non-contextual model. Contextual is used for multi-model and
        zero-shot experiments.
      params_dict: dictionary. Can store additional parameters and their values.
        This dictionary provides an easy and convenient way to add new hyper-
        parameters, via keys of this dictionary.
    """
    super().__init__()
    self.num_inputs = num_inputs
    self.num_outputs = num_outputs
    self.optimizer = optimizer
    self.params_dict = params_dict
    self.penalty_weight = penalty_weight
    self.contextual = contextual

    # Setting the following variable to True shouldn't cause issues since
    # it is not passed into the GradientTape, but better to be safe, and set
    # it to false if the variable is not used.

    # This variable determines the alpha multiplier in Equation 2.
    self.log_cql_alpha = tf.Variable(tf.math.log(self.penalty_weight + 1e-6),
                                     trainable=False)
    self.cql_alpha_value = tf.Variable(self.penalty_weight, trainable=False)

    self.negative_sampler = negative_sampler

    # In the paper, we use an evolutionary optimizer for obtaining adversarial
    # examples. However, unfortunately, this optimizer is proprietary, and so
    # we provide the example negative sampler that uses gradient ascent, similar
    # to conservative objective mocels https://arxiv.org/abs/2107.06882.
    self.num_gradient_infer_steps = 0
    if 'num_gradient_steps' in params_dict:
      self.num_gradient_infer_steps = params_dict['num_gradient_steps']

    self.opt_lr = 1e-3
    if 'opt_lr' in params_dict:
      self.opt_lr = params_dict['opt_lr']

    # the multiplier beta in Equation 3 in the paper.
    self.infeasible_alpha = 0.01
    if 'infeasible_alpha' in params_dict:
      self.infeasible_alpha = params_dict['infeasible_alpha']

    # Since the input to the model is a concatenation of one-hot values
    # representing each field, using the input_splits parameter, we partition
    # this big input vector into a list of one-hot vectors, one corresponding
    # to each discrete parameter.
    self.input_splits = None
    if 'input_splits' in params_dict:
      self.input_splits = params_dict['input_splits']

    # We use an architecture which resembles a mixture of experts, and so the
    # following parameter decides how many parameters we wish to have.
    self.num_votes = 1
    if 'num_votes' in params_dict:
      self.num_votes = params_dict['num_votes']

    # Whether to add dropout or not, in intermediate layers of the model, as
    # a means to prevent overfitting.
    use_dropout = False
    if 'use_dropout' in params_dict:
      use_dropout = params_dict['use_dropout']

    if self.contextual:
      """For contextual version of PRIME"""
      self.num_contexts = 0
      if 'num_contexts' in params_dict:
        self.num_contexts = params_dict['num_contexts']

    print('Infeasible alpha: ', self.infeasible_alpha)
    print('CQL Alpha: ', self.log_cql_alpha)
    print('Num votes: ', self.num_votes)

    self.input_layer = tf.keras.Input(num_inputs)
    temp_num_inputs = num_inputs

    # The following layer splits the input into a list of embeddings for
    # each parameter. Check the SplitEmbeddingLayer class for details.
    x = SplitEmbeddingLayer(softmax_splits=self.input_splits,
                            output_size=64)(self.input_layer)
    if use_dropout:
      x = tf.keras.layers.Dropout(rate=0.1)(x)

    # Now feed the split embedding layer output into TransformerLayer
    x = TransformerLayer(d_model=64, num_heads=8, dff=256)(x)
    x = TransformerLayer(d_model=64, num_heads=8, dff=256)(x)

    x = tf.keras.layers.Reshape(target_shape=(640,))(x)

    if self.contextual:
      context_input = tf.keras.Input(self.num_contexts)
      out_context = tf.keras.layers.Dense(640, use_bias=False)(context_input)

      # Pointwise multiply the contexts to make sure that the context
      # conditioning is done properly. From https://arxiv.org/abs/1912.13465.
      x = x * out_context
      self._base_network = tf.keras.Model(
        inputs=[self.input_layer, context_input], outputs=x)
    else:
      self._base_network = tf.keras.Model(
        inputs=self.input_layer, outputs=x)

    self.optimize_networks = [self._base_network, ]

    # Now feedforward layers to finish the model
    layers = list(layers)
    layers[0] = 64 * len(self.input_splits)

    """Voting based routing"""
    num_networks = self.num_votes
    self._all_networks = []
    for jdx in range(num_networks):
      # Make each of the networks used in routing
      new_network = tf.keras.Sequential()
      for idx in range(len(layers) - 1):
        new_network.add(
          tf.keras.layers.Dense(layers[idx + 1], input_shape=(layers[idx],)))
        new_network.add(tf.keras.layers.LeakyReLU(0.1))
        if use_dropout:
          new_network.add(tf.keras.layers.Dropout(rate=0.1))

      new_network.add(tf.keras.layers.Dense(
        num_outputs, input_shape=(layers[idx],)))
      self._all_networks.append(new_network)

    self.optimize_networks.extend(self._all_networks)

    # Now make the network that decides the contribution of these
    self.voting_network = tf.keras.Sequential()
    if self.contextual:
      self.voting_network.add(
        tf.keras.layers.Dense(layers[1], input_shape=(2 * layers[0],)))
    else:
      self.voting_network.add(
        tf.keras.layers.Dense(layers[1], input_shape=(layers[0],)))
    self.voting_network.add(tf.keras.layers.LeakyReLU(0.1))
    if use_dropout:
      self.voting_network.add(tf.keras.layers.Dropout(rate=0.1))

    self.voting_network.add(
      tf.keras.layers.Dense(self.num_votes, input_shape=(layers[1],)))

    if self.contextual:
      # Add the vote generation network input again
      self.embedding_network = tf.keras.Sequential()
      self.embedding_network.add(
        tf.keras.layers.Dense(256))
      self.embedding_network.add(tf.keras.layers.LeakyReLU(0.1))
      self.embedding_network.add(
        tf.keras.layers.Dense(layers[0]))

      self.optimize_networks.append(self.embedding_network)
    self.optimize_networks.append(self.voting_network)

    print('All networks: ', len(self.optimize_networks))

  @tf.function
  def call(self, inputs, training=True, with_logging=False):
    """Function to call one forward pass on the PRIME Transformer."""
    extra_dict = dict()
    if not self.contextual:
      transformer_embedding = self._base_network(inputs, training=training)
    else:
      # TODO(aviralkumar): Fix the hardcoded 77 input dimensionality in code
      if not isinstance(inputs, list) and not isinstance(inputs, tuple):
        inputs = (inputs[:, :77], inputs[:, 77:])

      transformer_embedding = self._base_network(inputs, training=training)

    # Get all outputs from each expert
    all_outputs = []
    for idx in range(self.num_votes):
      all_outputs.append(
        self._all_networks[idx](transformer_embedding, training=training))

    # Get the voting probabilities
    if self.contextual:
      vote_input = self.embedding_network(inputs[1])
      vote_input = tf.concat([transformer_embedding, vote_input], axis=-1)
      vote_logit = self.voting_network(vote_input, training=training)
    else:
      vote_logit = self.voting_network(transformer_embedding,
                                       training=training)

    # Append all_outputs in a list and compute average score
    all_outputs = tf.concat(all_outputs, axis=-1)  # [B x num_votes]
    vote_prob = tf.nn.softmax(vote_logit, axis=-1)  # [B x num_votes]
    vote_entropy = tf.reduce_sum(
      tf.nn.log_softmax(vote_logit, axis=-1) * vote_prob, axis=-1)
    extra_dict['vote_entropy'] = tf.reduce_mean(vote_entropy)
    fwd_model_pred = tf.reduce_sum(vote_prob * all_outputs, axis=-1)
    fwd_model_pred = tf.expand_dims(fwd_model_pred, axis=-1)

    if with_logging:
      return fwd_model_pred, extra_dict

    return fwd_model_pred

  def compute_loss(self, data_batch, loss_type='mse', training=True,
                   ranking_penalty_weight=0.0, inp_batch_type=None):
    """
    Compute the loss function and additional logging metrics for training.

    Args:
      data_batch: A dictionary of various input fields, and their corresponding
        tensor values. The keys for this dictionary are:
        - design --> denotes the input (accelerator config in this case)
        - objective --> denotes the objective value for the given input
        - context_id --> denotes the context vector for the case of contextual

      loss_type: string, either mse or mse+rank. It essentially computes the
        training loss used to train the PRIME model. We can optionally add some
        ranking regularization for training if needed. Though, we did not find
        this to be essential.

      inp_batch_type: string, either 'valid' or 'mixed'. Mixed indicates that
        the batch consists of both valid and invalid samples, whereas valid
        indicates the samples are only valid samples.

      ranking_penalty_weight: float, the weight on the ranking loss function
        in addition to the PRIME objectives. This is not needed for PRIME, but
        can help in some cases. So, leaving the facility here.
    """
    loss_dict = dict()
    if loss_type == 'mse':
      fwd_loss = weighted_mse_loss
    elif loss_type == 'mse+rank':
      fwd_loss = weighted_mse_loss
      ranking_loss_fn = ranking_trainable_loss

    loss_dict['y_values_max'] = tf.reduce_max(data_batch['objective'])
    loss_dict['y_values_mean'] = tf.reduce_mean(data_batch['objective'])

    data_batch = data_batch.copy()
    weights = tf.ones_like(data_batch['objective'])

    if self.contextual:
      model_pred, extra_dict = self(
        inputs=[data_batch['design'], data_batch['context_id']],
        training=training, with_logging=True)
    else:
      model_pred, extra_dict = self(
        data_batch['design'], training=training, with_logging=True)

    loss_dict.update(extra_dict)

    if self.negative_sampler is not None:
      # This branch of the code will not run off-the-shelf, since it assumes
      # access to a negative_sampler. A negative sampler is simply any kind of
      # optimizer that can take in the current PRIMETransformerModel and
      # optimize its predictions.
      negatives_batch = self.negative_sampler.run_inference(
        num_iters=2, model=self)
      negatives_pred = self(inputs=negatives_batch, training=training)
    else:
      negatives_batch = self.infer_negatives(data_batch)
      if self.contextual:
        negatives_pred = self(
          (negatives_batch['design'], negatives_batch['context_id']),
          training=training)
      else:
        negatives_pred = self(negatives_batch['design'], training=True)

    negatives_pred = tf.clip_by_value(negatives_pred, clip_value_min=-4000.0,
                                      clip_value_max=4000.0)

    cql_loss = tf.reduce_mean(negatives_pred)
    cql_loss = tf.clip_by_value(cql_loss,
                                clip_value_min=-4000,
                                clip_value_max=1e6)
    loss_dict['negatives_dist'] = tf.reduce_mean(negatives_pred)

    mse_loss = weighted_mse_loss(
      model_pred, data_batch['objective'], weights)

    if loss_type == 'mse+rank':
      if self.contextual:
        avg_ranking_train_loss = ranking_loss_fn(
          model_pred, data_batch['objective'],
          context=data_batch['raw_context'])
      else:
        avg_ranking_train_loss = ranking_loss_fn(
          model_pred, data_batch['objective'])
    else:
      avg_ranking_train_loss = 0.0

    # Only used for logging, measures how big the MSE error is relative to
    # the output of the model.
    avg_approx_loss = weighted_approx_loss(
      model_pred, data_batch['objective'], weights)
    passed_context = None

    if self.contextual:
      passed_context = data_batch['raw_context']

    avg_ranking_loss = ranking_loss(
      model_pred, data_batch['objective'], context=passed_context)
    avg_kendall_loss = kendall_correlation(
      model_pred, data_batch['objective'], context=passed_context)

    train_loss = mse_loss
    loss_dict['mse_loss'] = mse_loss
    loss_dict['avg_approx_loss'] = avg_approx_loss
    loss_dict['avg_ranking_loss'] = avg_ranking_loss
    loss_dict['avg_ranking_train_loss'] = avg_ranking_train_loss
    loss_dict['avg_kendall_loss'] = avg_kendall_loss
    loss_dict['cql_loss'] = cql_loss
    loss_dict['negatives_pred'] = tf.reduce_mean(negatives_pred)
    loss_dict['model_pred_average'] = tf.reduce_mean(model_pred)
    train_loss = train_loss + ranking_penalty_weight * avg_ranking_train_loss
    train_loss = train_loss + self.cql_alpha_value * cql_loss

    if inp_batch_type is not 'valid':
      weights_negatives = tf.ones_like(data_batch['objective'])
      if self.contextual:
        model_pred_invalid, invalid_dict = self(
          inputs=(data_batch['invalid/design'], data_batch['context_id']),
          training=training, with_logging=True)
      else:
        model_pred_invalid, invalid_dict = self(
          data_batch['invalid/design'], training=training, with_logging=True)

      for key in invalid_dict:
        loss_dict['invalid/' + key] = invalid_dict[key]

      ## Conservatism training
      loss_dict['y_value_infeasible'] = tf.reduce_mean(model_pred_invalid)
      loss_dict['y_value_infeasible'] = tf.clip_by_value(
        loss_dict['y_value_infeasible'],
        clip_value_min=-1000, clip_value_max=1e6)
      train_loss = train_loss + self.infeasible_alpha * \
                   loss_dict['y_value_infeasible']

      mse_loss_invalid = weighted_mse_loss(
        model_pred_invalid, data_batch['invalid/objective'],
        weights_negatives)
      avg_approx_loss_invalid = weighted_approx_loss(
        model_pred_invalid, data_batch['invalid/objective'],
        weights_negatives)
      mse_loss = mse_loss + mse_loss_invalid
      loss_dict['mse_loss_invalid'] = mse_loss_invalid
      loss_dict['mse_loss_overall'] = mse_loss
      loss_dict['avg_approx_loss_invalid'] = avg_approx_loss_invalid
    return loss_dict, train_loss

  def perform_training(self, batch, loss_type,
                       ranking_penalty_weight=0.0, **kwargs):
    """
    Actually perform training by computing loss, and then taking gradients
    through it. Makes sure to backpropagate through all networks.
    """
    with tf.GradientTape(
            watch_accessed_variables=False, persistent=True) as tape:
      tape.watch(
        [v for net in self.optimize_networks \
         for v in net.trainable_variables])
      loss_dict, loss_train = self.compute_loss(
        batch, loss_type, training=True,
        ranking_penalty_weight=ranking_penalty_weight)

    grads = tape.gradient(loss_train,
                          [v for net in self.optimize_networks \
                           for v in net.trainable_variables])
    gen_grads_op = self.optimizer.apply_gradients(
      zip(grads, [v for net in self.optimize_networks \
                  for v in net.trainable_variables]))
    return loss_dict

  def measure_stats(self, batch, batch_type=None, **kwargs):
    """Simply make a forward pass through compute_loss to measure losses."""
    loss_dict, _ = self.compute_loss(batch, loss_type='mse+rank',
                                     training=False,
                                     inp_batch_type=batch_type)
    return loss_dict

  def infer_negatives(self, batch):
    """Run gradient descent to obtain negative examples"""
    temp_batch = dict()
    log_probs = batch['design']
    if self.contextual:
      contexts = batch['context_id']
    for _ in range(self.num_gradient_infer_steps):
      with tf.GradientTape(
              watch_accessed_variables=False, persistent=False) as tape:
        tape.watch(log_probs)
        if self.contextual:
          model_pred = self((log_probs, contexts), training=False)
        else:
          model_pred = self(log_probs, training=False)
      grad = tape.gradient(model_pred, log_probs)
      log_probs = log_probs + self.opt_lr * grad[0]
    temp_batch['design'] = tf.stop_gradient(log_probs)
    if 'context_id' in batch and self.contextual:
      temp_batch['context_id'] = batch['context_id']
    return temp_batch


#@title Define the hardware optimization problem

class HardwareOptProblem:
  """
  Problem for loading the task dataset and training
  """
  def __init__(self,
               config: dict,
               data_file: dict,
               params_dict: Optional[dict] = None):
    """Initialize a hardware optimization problem.

    config: a dictionary of various input fields and their corresponding
      possible valid number of discrete values.
    data_file: a dictionary of a list of various input fields.
    params_dict: a dictionary of additional inputs to the HardwareOptProblem.
    """

    # Batch size for the batch sampling
    self.batch_size = 256
    if 'batch_size' in params_dict:
      self._batch_size = params_dict['batch_size']

    # Whether to train on infeasible points or not
    # use 'valid' for feasible points, and 'mixed' for both infeasible and
    # feasible points
    self._batch_type = 'valid'
    if 'batch_type' in params_dict:
      self._batch_type = params_dict['batch_type']

    # Add any area constraints or not: this flag enables filtering the data
    # basedn on whether the area constraint is not satisfied
    self._add_area_constraints = False
    if 'add_area_constraints' in params_dict:
      self._add_area_constraints = params_dict['add_area_constraints']

    self.dataset = PRIMEDataset(config=config,
                                data_dict=data_file)
    self.feasible_probs,\
             self.infeasible_probs = self.dataset.get_feasible_probs(
                              add_area_constraints=self._add_area_constraints)

    # Choose what kind of batch to provide while training the model
    self.get_training_batch = None
    if self._batch_type == 'valid':
      self.get_training_batch = self.get_valid_only_batch
    elif self._batch_type == 'mixed':
      self.get_training_batch = self.get_mixed_batch
    else:
      self.get_training_batch = self.get_all_batch

  def get_all_batch(self,):
    """Sample i.i.d. from the entire dataset."""
    indices = np.random.randint(1,
                                self.dataset._top, self._batch_size)
    batch_x, batch_y = self.dataset._get_batch(indices)
    batch_dict = dict()
    batch_dict['design'] = batch_x
    batch_dict['objective'] = batch_y
    return batch_dict

  def get_valid_only_batch(self,):
    """Get only valid samples in the batch."""
    indices = np.random.choice(np.arange(0, self.dataset._top),
                               size=self._batch_size, p=self.feasible_probs)
    batch_x, batch_y = self.dataset._get_batch(indices)
    batch_dict = dict()
    batch_dict['design'] = batch_x
    batch_dict['objective'] = batch_y
    return batch_dict

  def get_top_batch(self,):
    """Get only the top scoring batch for eval"""
    indices = self.dataset._tf_dataset['argsort'][-self.batch_size:]
    batch_x, batch_y = self.dataset._get_batch(indices)
    batch_dict = dict()
    batch_dict['design'] = batch_x
    batch_dict['objective'] = batch_y
    return batch_dict

  def get_mixed_batch(self,):
    """Get both valid and invalid samples to train in a batch"""
    # Should be called when training with invalid samples as negatives
    valid_indices = np.random.choice(np.arange(0, self.dataset._top),
                                     size=self._batch_size,
                                     p=self.feasible_probs)
    invalid_indices = np.random.choice(np.arange(0, self.dataset._top),
                                       size=self._batch_size,
                                       p=self.infeasible_probs)
    batch_x, batch_y = self.dataset._get_batch(valid_indices)
    batch_x_in, batch_y_in = self.dataset._get_batch(invalid_indices)
    batch_dict = dict()
    batch_dict['design'] = batch_x
    batch_dict['objective'] = batch_y
    batch_dict['invalid/design'] = batch_x_in
    batch_dict['invalid/objective'] = batch_y_in
    return batch_dict


# @title Define the dataset

class PRIMEDataset(tf.Module):
  """
  Load the dataset to be able to train the PRIMETransformerModel.
  """

  def __init__(self,
               config,
               data_dict: dict,
               **kwargs):
    """Create a dataset for training PRIME."""
    self._config = config

    self.data_dict = data_dict
    self._design_space_dict = {}
    self._segment_lengths = {}
    self._max_ctr = 0
    self._eval_metric_keys = ['area', 'runtime', 'score']
    self._validity_keys = ['infeasible', ]

    self._active_training_keys = ['param_1', 'param_2', 'param_3',
                                  'param_4', 'param_5', 'param_6',
                                  'param_7', 'param_8', 'param_9',
                                  'param_10']

    self._tf_dataset = {}
    self._top = 0
    if self.data_dict is not None:
      self._setup_dataset()

  def _setup_dataset(self, ):
    """Main function to setup the dataset"""
    self.load_or_refresh_config()
    logging.info('Loading dataset..')
    self._convert_to_tf_dataset()
    self.get_score_function()
    print('Loaded dataset....', self.size)

  def get_input_splits(self, ):
    """Get the splits of input of the dataset."""
    lengths = []
    for key in self._active_training_keys:
      ctr_idx = self._design_space_dict[key]['ctr']
      lengths.append(self._segment_lengths[ctr_idx])
    self._active_lengths = lengths
    return lengths

  def get_score_function(self, ):
    """Get the objective function which is being maximized"""
    runtime = self._tf_dataset['runtime'].numpy()
    area = self._tf_dataset['area'].numpy()
    scores = -runtime
    self._tf_dataset['score'] = tf.convert_to_tensor(
      scores, dtype=tf.float32)
    print('Score stats: ')
    print('--------------------------------------------')
    print('Max: ', scores.max())
    print('Mean: ', scores.mean())
    print('Min: ', scores.min())
    print('--------------------------------------------')

    # Since we need top batch for eval, store top scores
    self._tf_dataset['argsort'] = np.argsort(
      self._tf_dataset['score'].numpy())
    return scores

  def _convert_to_tf_dataset(self, ):
      """Convert the dataset to a tensorflow dataset, easy to read from."""
      tf_dataset = {}
      for key in self._active_training_keys + \
                 self._eval_metric_keys + self._validity_keys:
          tf_dataset[key] = []

      # Load the data from the data file. Note that most of the fields are
      # actually not one-hots, and essentially corresponds to the original data
      # with field-value pairs for each field, and the value is a discrete value.
      parsed_dataset = self.data_dict
      for p in parsed_dataset:
          for key in p:
              if key not in tf_dataset:
                  continue
              tf_dataset[key].append(p[key])

      for key in self._active_training_keys + self._eval_metric_keys + self._validity_keys:
          if key == 'infeasibility_reason':
              continue
          tf_dataset[key] = tf.convert_to_tensor(tf_dataset[key], tf.int32)

      # Now convert the dataset to actually use one-hot representations. This is
      # used for training, and so it is important to use this.
      tf_actual_temp_dataset = {}
      for key in self._active_training_keys + self._validity_keys + self._eval_metric_keys:
          tf_actual_temp_dataset[key] = tf.cast(tf_dataset[key], dtype=tf.int32)

      for key in self._active_training_keys:
          design_space_map = dict(
              self._design_space_dict[key]['mapping_one_hot_to_value'])
          data_val = tf_actual_temp_dataset[key].numpy().astype(np.int32).tolist()
          out_vals = []
          for x in data_val:
              out_vals.append(design_space_map[x])

          tf_actual_temp_dataset[key] = tf.constant(out_vals, dtype=tf.int32)

      ## Finally load the tf_actual_temp_dataset into the tf_dataset
      tf_actual_dataset = {}
      for key in tf_actual_temp_dataset:
          tf_actual_dataset[key] = tf_actual_temp_dataset[key]

      self._tf_dataset = tf_actual_dataset
      self._infeasible_np = self._tf_dataset['infeasible'].numpy().astype(
          np.float32)
      self._top = self._infeasible_np.shape[0]

  def load_or_refresh_config(self):
    """Load config file with specifications."""
    self._design_space_dict = {}
    self._segment_lengths = {}

    try:
      # The case when the config is a file to open
      with gfile.Open(self._config, 'r') as f:
        line = f.readline()
        line = line.replace('\n', '')
        # print ('Line: ', line)
        ctr = 0
        while line:
          ind_field = dict()
          split_line = line.split(':')
          ind_field['data_type'] = split_line[0]
          ind_field['value_range'] = [int(x) for x in split_line[-1].split(',')]
          index_vals = np.arange(len(ind_field['value_range']))
          ind_field['mapping_one_hot_to_value'] = zip(
            ind_field['value_range'], index_vals)
          ind_field['ctr'] = ctr
          self._design_space_dict[split_line[1]] = ind_field
          self._segment_lengths[ctr] = len(ind_field['value_range'])
          self._max_ctr += 1
          line = f.readline()
          ctr += 1
    except:
      # When config is a string of the contents of the file
      lines = self._config.split("\n")
      lines = [line.replace('\n', '') for line in lines]
      ctr = 0
      for line in lines:
        ind_field = dict()
        split_line = line.split(':')
        ind_field['data_type'] = split_line[0]
        ind_field['value_range'] = [int(x) for x in split_line[-1].split(',')]
        index_vals = np.arange(len(ind_field['value_range']))
        ind_field['mapping_one_hot_to_value'] = zip(
          ind_field['value_range'], index_vals)
        ind_field['ctr'] = ctr
        self._design_space_dict[split_line[1]] = ind_field
        self._segment_lengths[ctr] = len(ind_field['value_range'])
        self._max_ctr += 1
        ctr += 1

    split_lengths = []
    for key in self._active_training_keys:
      split_lengths.append(
        self._segment_lengths[self._design_space_dict[key]['ctr']])
    total_length_split = 0

    if total_length_split > 0:
      split_lengths.append(total_length_split)
    self.split_lengths = split_lengths  # later used to split input when needed
    self.continuous_or_not = (total_length_split > 0)

  @property
  def size(self, ):
    return self._top

  @property
  def input_properties(self):
    """Get the total length of the vector to be fed as input to the model."""
    length = 0
    for val in self._active_lengths:
      length += val
    return length

  def get_feasible_probs(self, add_area_constraints=False):
    """
    Get the probability of points that are feasible, meaning they don't
    violate the area constraint and also obtain the feasibility result.
    """
    feasible = (1.0 - self._infeasible_np)
    print('Number of feasible points: ', np.sum(feasible))
    if add_area_constraints:
      print('Min area: ', tf.reduce_min(self._tf_dataset['area']))
      feasible_area = (
              self._tf_dataset['area'] <= AREA_THRESHOLD).numpy().astype(np.float32)
      feasible = np.clip(feasible + feasible_area - 1.0,
                         a_min=0.0, a_max=1.0)
      print('Number of feasible points due to area constraint: ',
            np.sum(feasible_area))
      print('NUmber of feasible points after area constraint: ',
            np.sum(feasible))
    probs = feasible / np.sum(feasible)
    infeasible_probs = (1.0 - feasible) / np.sum(1.0 - feasible)
    return probs, infeasible_probs

  def valid_invalid_data_size(self, add_area_constraints=True):
    """Get the size of the valid and invalid dataset compositions."""
    feasible = (1.0 - self._infeasible_np)
    if add_area_constraints:
      feasible_area = (
              self._tf_dataset['area'] <= AREA_THRESHOLD).numpy().astype(np.float32)
      feasible = np.clip(feasible + feasible_area - 1.0,
                         a_min=0.0, a_max=1.0)
    return np.sum(feasible), np.shape(feasible)[0] - np.sum(feasible)

  def _get_batch(self, indices):
    """Sample a batch from the dataset."""
    all_train_elements = []  # this is the training elements in one-hot form
    all_test_elements = []  # this is the evaluation fields (area, runtime, score, etc)

    # Discrete training input keys
    for key in self._active_training_keys:
      all_train_elements.append(
        tf.one_hot(tf.gather(self._tf_dataset[key], indices),
                   depth=self._segment_lengths[
                     self._design_space_dict[key]['ctr']]))

    # Eval keys
    all_test_elements = tf.expand_dims(
      tf.gather(self._tf_dataset['score'], indices), 1)
    return tf.concat(all_train_elements, 1), all_test_elements


# @title Defining the function that runs training

def train_eval_offline(
        # Data flags
        config=None,
        training_dataset=None,
        validation_dataset=None,
        # Train flags
        train_steps=int(1e6),
        summary_freq=1000,
        eval_freq=1000,
        # Train hparams
        add_summary=True,
        save_dir=None,
        loss_type='mse',
        layers=(512, 512, 512),
        opt_lr=1e-4,
        opt_betas=(0.9, 0.999),
        with_ranking_penalty=False,
        ranking_penalty_weight=0.1,
        batch_size=256,
        batch_type='mixed',
        # params of the model
        use_dropout=False,
        num_votes=1,
        # PRIME parameters:
        cql_alpha=1.0,
        infeasible_alpha=1.0):
  """Training loop for the PRIME model.

  Most of the input arguments are primarily hyperparameters for training the
  PRIME model, and self explanatory. Other arguments explained below.

  save_dir: the directory where the store the saved model, and the training
    summaries. Can be a string or None.
  training_dataset: a dictionary of fields in the training dataset, and their
    corresponding values used to train.
  validation_dataset: a dictionary of fields in the validation dataset, and
    their corresponding values to measure cross-validation.
  """

  # First create the training dataset, note that the dataset below is a
  # dummy dataset, that is only well-suited for training as a representative
  # example. You can plug in the dataset from the other colab that provides
  # the data for training, or you can add your own dataset here.
  params_dict = dict()
  params_dict['batch_size'] = batch_size
  params_dict['batch_type'] = batch_type
  params_dict['add_area_constraints'] = True
  # Defining the problem automatically does dataset loading
  train_problem = HardwareOptProblem(config,
                                     training_dataset, params_dict)

  # Now define the validation dataset (or val_problem)
  val_params_dict = dict()
  val_params_dict['batch_size'] = batch_size
  val_params_dict['add_area_constraints'] = True
  # Only validate on the valid samples in the validation dataset
  val_params_dict['batch_type'] = 'valid'
  val_problem = HardwareOptProblem(config, validation_dataset,
                                   val_params_dict)

  # The dimensionality of each parameter. this input_splits parameter goes
  # into the PRIMETransformer, as it enables us to pass in inputd as a big
  # vector of concatenated one-hot vectors for each discrete parameter, and
  # then unpack it in the model training. This gives the flexibility of actually
  # being able to use the input one-hot vectors in any way as needed.
  input_splits = train_problem.dataset.get_input_splits()
  print('Input splits: ', input_splits)

  # Number of inputs in all: the total dimensionality of the input is given by
  # the sum of number of possible values each discrete parameter can take
  input_properties = train_problem.dataset.input_properties
  print('Loaded validation dataset..', train_problem.dataset.size,
        val_problem.dataset.size, input_properties)

  feasible_size, \
  infeasible_size = train_problem.dataset.valid_invalid_data_size()
  print('Feasible/Infeasible size: ', feasible_size, infeasible_size)

  fwd_optimizer = tf.keras.optimizers.Adam(learning_rate=opt_lr,
                                           beta_1=opt_betas[0],
                                           beta_2=opt_betas[1], name='opt')

  training_dict = dict()
  training_dict['training_type'] = batch_type
  training_dict['use_dropout'] = use_dropout
  training_dict['infeasible_alpha'] = infeasible_alpha
  training_dict['input_splits'] = input_splits
  training_dict['num_votes'] = num_votes
  training_dict['infeasbile_multiplier'] = float(feasible_size) / (
          float(infeasible_size) + 1)
  training_dict['num_gradient_steps'] = 20

  model = PRIMETransformerModel(
    num_outputs=1,
    num_inputs=input_properties,
    optimizer=fwd_optimizer,
    layers=layers,
    penalty_weight=cql_alpha,
    params_dict=training_dict)

  rand_num = np.random.randint(10000)

  # summary writer
  if save_dir is not None:
    save_dir = os.path.join(save_dir, str(rand_num))
    summary_writer = tf.summary.create_file_writer(logdir=save_dir)
    summary_writer.set_as_default()
  else:
    tf.summary.create_noop_writer()

  print('save dir : ', save_dir)

  # Now start the training
  for step in range(train_steps):
    batch = train_problem.get_training_batch()
    # This is just to build the models.
    if step == 0:
      _ = model.measure_stats(batch)
    loss_dict = model.perform_training(
      batch, loss_type=loss_type,
      ranking_penalty_weight=ranking_penalty_weight)

    if step % summary_freq == 0:
      # regular logging
      print('-------------------------------------------------------')
      for key in loss_dict:
        tf.summary.scalar('train/' + key, loss_dict[key], step=step)
        print('Step: ', step, 'train/' + key, ':', loss_dict[key])
      print('-------------------------------------------------------')

      if save_dir is not None:
        if step == 0:
          model.save(save_dir)
        if step % 5000 == 0:
          model.save_weights(os.path.join(save_dir, "ckpt-" + str(step)))

    if step % eval_freq == 0:
      val_batch = val_problem.get_training_batch()
      # validation batches are only valid batches
      val_loss_dict = model.measure_stats(val_batch, batch_type='valid')
      print('-------------------------------------------------------')
      for key in val_loss_dict:
        tf.summary.scalar('val/' + key, val_loss_dict[key], step=step)
        print('Step: ', step, 'val/' + key, ':', val_loss_dict[key])
      print('-------------------------------------------------------')

  print('Finished Training')



#@title Importing the necessary libraries
import tensorflow as tf
import numpy as np
#@title APIs for parsing PRIME datasets
def parse_prime_tfrecords(proto):
  prime_feature_description = {
    'param_1': tf.io.FixedLenFeature([], tf.float32),
    'param_2': tf.io.FixedLenFeature([], tf.float32),
    'param_3': tf.io.FixedLenFeature([], tf.float32),
    'param_4': tf.io.FixedLenFeature([], tf.float32),
    'param_5': tf.io.FixedLenFeature([], tf.float32),
    'param_6': tf.io.FixedLenFeature([], tf.float32),
    'param_7': tf.io.FixedLenFeature([], tf.float32),
    'param_8': tf.io.FixedLenFeature([], tf.float32),
    'param_9': tf.io.FixedLenFeature([], tf.float32),
    'param_10': tf.io.FixedLenFeature([], tf.float32),
    'runtime': tf.io.FixedLenFeature([], tf.float32),
    'area': tf.io.FixedLenFeature([], tf.float32),
    'infeasible':tf.io.FixedLenFeature([], tf.int64),
  }
  return tf.io.parse_single_example(proto, prime_feature_description)


#@title Parsing the dataset for the studied application
model_name = 'm4' #@param ["MobilenetEdgeTPU", "MobilenetV2", "MobilenetV3", "m4", "m5", "m6", "t_rnn_dec", "t_rnn_enc", "u-net"]
filenames = tf.io.gfile.glob(f'gs://gresearch/prime/{model_name}/*.tfrecord')
raw_dataset = tf.data.TFRecordDataset(filenames, num_parallel_reads=64)
parsed_dataset = raw_dataset.map(parse_prime_tfrecords)
filenames = tf.io.gfile.glob(f'gs://gresearch/prime/{model_name}/*.tfrecord')

config_str = """discrete:param_1:float64:true:1,2,4,6,8,10,12,14,16,32
discrete:param_2:float64:true:1,2,4,6,8,10,12,14,16,32
discrete:param_3:float64:true:4,8,16,32,64,128,256
discrete:param_7:float64:true:256,512,1024,2048,4096,8192,16384
discrete:param_8:float64:true:8192,16384,32768,65536
discrete:param_9:float64:true:2048,4096,8192,16384,32768
discrete:param_6:float64:true:4096,8192,16384,32768,65536,131072,262144,524288,1048576,2097152,4194304
discrete:param_5:float64:true:262144,524288,1048576,2097152,4194304,8388608,16777216
discrete:param_4:float64:true:1,2,4,6,8,10,12,14,16,32
discrete:param_10:float64:true:5,10,16,20,25,30"""

# Generating dummy data
temp_dataset = PRIMEDataset(config=config_str, data_dict=parsed_dataset)

#@title Reproducing the data in the Table 1
training_data_dict = {}
number_of_infeasibles = 0
number_of_feasibles = 0
latency = []
values = ['param_1', 'param_2', 'param_3', 'param_7', 'param_8', 'param_9', 'param_6', 'param_5', 'param_4', 'param_10', 'infeasible', 'runtime', 'area']
for value in values:
  training_data_dict[value] = []

#@title Running training

# A toy example of running training PRIME.
train_eval_offline(
    config=config_str,
    training_dataset=parsed_dataset,
    validation_dataset=parsed_dataset,
    train_steps=100,
    summary_freq=10,
    eval_freq=10,
    add_summary=True,
    save_dir=None,
    loss_type='mse+rank',
    layers=(256, 256, 256),
    with_ranking_penalty=True,
    ranking_penalty_weight=0.01,
    use_dropout=True,
    cql_alpha=0.1,
    infeasible_alpha=0.05
)


