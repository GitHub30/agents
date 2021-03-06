# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
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

"""Tests for TF Agents ppo_agent."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl.testing import parameterized
import mock
import numpy as np
import tensorflow as tf

from tf_agents.agents.ppo import ppo_agent
from tf_agents.environments import time_step as ts
from tf_agents.environments import trajectory
from tf_agents.networks import actor_distribution_network
from tf_agents.networks import network
from tf_agents.networks import utils as network_utils
from tf_agents.networks import value_network
from tf_agents.specs import tensor_spec

slim = tf.contrib.slim
nest = tf.contrib.framework.nest


class DummyActorNet(network.Network):

  def __init__(self, action_spec, name=None):
    super(DummyActorNet, self).__init__(name, None, (), 'DummyActorNet')
    self._action_spec = action_spec
    self._flat_action_spec = nest.flatten(self._action_spec)[0]
    self._outer_rank = 1  # TOOD(oars): Do we need this?

    self._layers.append(
        tf.keras.layers.Dense(
            self._flat_action_spec.shape.num_elements() * 2,
            kernel_initializer=tf.constant_initializer([[2, 1], [1, 1]]),
            bias_initializer=tf.constant_initializer([5, 5]),
            activation=None,
        ))

  def call(self, inputs, unused_step_type=None, network_state=()):
    hidden_state = tf.to_float(nest.flatten(inputs))[0]
    batch_squash = network_utils.BatchSquash(self._outer_rank)
    hidden_state = batch_squash.flatten(hidden_state)

    for layer in self.layers:
      hidden_state = layer(hidden_state)

    actions, stdevs = tf.split(hidden_state, 2, axis=1)
    actions = batch_squash.unflatten(actions)
    stdevs = batch_squash.unflatten(stdevs)
    actions = nest.pack_sequence_as(self._action_spec, [actions])
    stdevs = nest.pack_sequence_as(self._action_spec, [stdevs])
    return nest.map_structure_up_to(self._action_spec, tf.distributions.Normal,
                                    actions, stdevs), network_state


class DummyValueNet(network.Network):

  def __init__(self, name=None, outer_rank=1):
    super(DummyValueNet, self).__init__(name, None, (), 'DummyValueNet')
    self._outer_rank = outer_rank
    self._layers.append(
        tf.keras.layers.Dense(
            1,
            kernel_initializer=tf.constant_initializer([2, 1]),
            bias_initializer=tf.constant_initializer([5])))

  def call(self, inputs, unused_step_type=None, network_state=()):
    hidden_state = tf.to_float(nest.flatten(inputs))[0]
    batch_squash = network_utils.BatchSquash(self._outer_rank)
    hidden_state = batch_squash.flatten(hidden_state)
    for layer in self.layers:
      hidden_state = layer(hidden_state)
    value_pred = tf.squeeze(batch_squash.unflatten(hidden_state), axis=-1)
    return value_pred, network_state


def _compute_returns_fn(rewards, discounts, next_state_return=0.0):
  """Python implementation of computing discounted returns."""
  returns = np.zeros_like(rewards)
  for t in range(len(returns) - 1, -1, -1):
    returns[t] = rewards[t] + discounts[t] * next_state_return
    next_state_return = returns[t]
  return returns


class PPOAgentTest(parameterized.TestCase, tf.test.TestCase):

  def setUp(self):
    super(PPOAgentTest, self).setUp()
    self._obs_spec = tensor_spec.TensorSpec([2], tf.float32)
    self._time_step_spec = ts.time_step_spec(self._obs_spec)
    self._action_spec = tensor_spec.BoundedTensorSpec([1], tf.float32, -1, 1)

  def testCreateAgent(self):
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec),
        check_numerics=True)
    agent.initialize()

  def testComputeAdvantagesNoGae(self):
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec),
        value_net=DummyValueNet(),
        normalize_observations=False,
        use_gae=False)
    rewards = tf.constant([[1.0] * 9, [1.0] * 9])
    discounts = tf.constant([[1.0, 1.0, 1.0, 1.0, 0.0, 0.9, 0.9, 0.9, 0.0],
                             [1.0, 1.0, 1.0, 1.0, 0.0, 0.9, 0.9, 0.9, 0.0]])
    returns = tf.constant([[5.0, 4.0, 3.0, 2.0, 1.0, 3.439, 2.71, 1.9, 1.0],
                           [3.0, 4.0, 7.0, 2.0, -1.0, 5.439, 2.71, -2.9, 1.0]])
    value_preds = tf.constant([
        [3.0] * 10,
        [3.0] * 10,
    ])  # One extra for final time_step.

    expected_advantages = returns - value_preds[:, :-1]
    advantages = agent.compute_advantages(rewards, returns, discounts,
                                          value_preds)
    self.assertAllClose(expected_advantages, advantages)

  def testComputeAdvantagesWithGae(self):
    gae_lambda = 0.95
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec,),
        value_net=DummyValueNet(),
        normalize_observations=False,
        use_gae=True,
        lambda_value=gae_lambda)
    rewards = tf.constant([[1.0] * 9, [1.0] * 9])
    discounts = tf.constant([[1.0, 1.0, 1.0, 1.0, 0.0, 0.9, 0.9, 0.9, 0.0],
                             [1.0, 1.0, 1.0, 1.0, 0.0, 0.9, 0.9, 0.9, 0.0]])
    returns = tf.constant([[5.0, 4.0, 3.0, 2.0, 1.0, 3.439, 2.71, 1.9, 1.0],
                           [5.0, 4.0, 3.0, 2.0, 1.0, 3.439, 2.71, 1.9, 1.0]])
    value_preds = tf.constant([[3.0] * 10,
                               [3.0] * 10])  # One extra for final time_step.

    gae_vals = tf.constant([[
        2.0808625, 1.13775, 0.145, -0.9, -2.0, 0.56016475, -0.16355, -1.01, -2.0
    ], [
        2.0808625, 1.13775, 0.145, -0.9, -2.0, 0.56016475, -0.16355, -1.01, -2.0
    ]])
    advantages = agent.compute_advantages(rewards, returns, discounts,
                                          value_preds)
    self.assertAllClose(gae_vals, advantages)

  @parameterized.named_parameters([
      ('OneEpoch', 1),
      ('FiveEpochs', 5),
  ])
  def testTrain(self, num_epochs):
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec,),
        value_net=DummyValueNet(outer_rank=2),
        normalize_observations=False,
        num_epochs=num_epochs,
    )
    observations = tf.constant([
        [[1, 2], [3, 4], [5, 6]],
        [[1, 2], [3, 4], [5, 6]],
    ],
                               dtype=tf.float32)
    time_steps = ts.TimeStep(
        step_type=tf.constant([[1] * 3] * 2, dtype=tf.int32),
        reward=tf.constant([[1] * 3] * 2, dtype=tf.float32),
        discount=tf.constant([[1] * 3] * 2, dtype=tf.float32),
        observation=observations)
    actions = tf.constant([[[0], [1], [1]], [[0], [1], [1]]], dtype=tf.float32)
    action_distribution_parameters = {
        'loc': tf.constant([[0.0, 0.0], [0.0, 0.0]], dtype=tf.float32),
        'scale': tf.constant([[1.0, 1.0], [1.0, 1.0]], dtype=tf.float32),
    }
    policy_info = action_distribution_parameters

    experience = trajectory.Trajectory(
        time_steps.step_type, observations, actions, policy_info,
        time_steps.step_type, time_steps.reward, time_steps.discount)

    # Mock the build_train_op to return an op for incrementing this counter.
    counter = tf.train.get_or_create_global_step()
    zero = tf.constant(0, dtype=tf.float32)
    agent.build_train_op = (
        lambda *_, **__: (counter.assign_add(1), [zero] * 5))

    train_op = agent.train(experience)

    with self.test_session() as sess:
      sess.run(tf.global_variables_initializer())

      # Assert that counter starts out at zero.
      counter_ = sess.run(counter)
      self.assertEqual(0, counter_)

      sess.run(train_op)

      # Assert that train_op ran increment_counter num_epochs times.
      counter_ = sess.run(counter)
      self.assertEqual(num_epochs, counter_)

  def testBuildTrainOp(self):
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec,),
        value_net=DummyValueNet(),
        normalize_observations=False,
        normalize_rewards=False,
        value_pred_loss_coef=1.0,
        policy_l2_reg=1e-4,
        value_function_l2_reg=1e-4,
        entropy_regularization=0.1,
        importance_ratio_clipping=10,
    )
    observations = tf.constant([[1, 2], [3, 4], [1, 2], [3, 4]],
                               dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([[0], [1], [0], [1]], dtype=tf.float32)
    returns = tf.constant([1.9, 1.0, 1.9, 1.0], dtype=tf.float32)
    sample_action_log_probs = tf.constant([0.9, 0.3, 0.9, 0.3],
                                          dtype=tf.float32)
    advantages = tf.constant([1.9, 1.0, 1.9, 1.0], dtype=tf.float32)
    valid_mask = tf.constant([1.0, 1.0, 0.0, 0.0], dtype=tf.float32)
    sample_action_distribution_parameters = {
        'loc': tf.constant([[9.0], [15.0], [9.0], [15.0]], dtype=tf.float32),
        'scale': tf.constant([[8.0], [12.0], [8.0], [12.0]], dtype=tf.float32),
    }
    train_step = tf.train.get_or_create_global_step()

    (train_op, losses) = (
        agent.build_train_op(
            time_steps,
            actions,
            sample_action_log_probs,
            returns,
            advantages,
            sample_action_distribution_parameters,
            valid_mask,
            train_step,
            summarize_gradients=False,
            gradient_clipping=0.0,
            debug_summaries=False))
    (policy_gradient_loss, value_estimation_loss, l2_regularization_loss,
     entropy_reg_loss, kl_penalty_loss) = losses

    # Run train_op once.
    self.evaluate(tf.global_variables_initializer())
    total_loss_, pg_loss_, ve_loss_, l2_loss_, ent_loss_, kl_penalty_loss_ = (
        self.evaluate([
            train_op, policy_gradient_loss, value_estimation_loss,
            l2_regularization_loss, entropy_reg_loss, kl_penalty_loss
        ]))

    # Check loss values are as expected. Factor of 2/4 is because four timesteps
    # were included in the data, but two were masked out. Reduce_means in losses
    # will divide by 4, but computed loss values are for first 2 timesteps.
    expected_pg_loss = -0.0164646133 * 2 / 4
    expected_ve_loss = 123.205 * 2 / 4
    expected_l2_loss = 1e-4 * 12 * 2 / 4
    expected_ent_loss = -0.370111 * 2 / 4
    expected_kl_penalty_loss = 0.0
    self.assertAllClose(
        expected_pg_loss + expected_ve_loss + expected_l2_loss +
        expected_ent_loss + expected_kl_penalty_loss,
        total_loss_,
        atol=0.001,
        rtol=0.001)
    self.assertAllClose(expected_pg_loss, pg_loss_)
    self.assertAllClose(expected_ve_loss, ve_loss_)
    self.assertAllClose(expected_l2_loss, l2_loss_, atol=0.001, rtol=0.001)
    self.assertAllClose(expected_ent_loss, ent_loss_)
    self.assertAllClose(expected_kl_penalty_loss, kl_penalty_loss_)

    # Assert that train_step was incremented
    self.assertEqual(1, self.evaluate(train_step))

  def testDebugSummaries(self):
    logdir = self.get_temp_dir()
    with tf.contrib.summary.create_file_writer(
        logdir,
        max_queue=None,
        flush_millis=None,
        filename_suffix=None,
        name=None).as_default():
      agent = ppo_agent.PPOAgent(
          self._time_step_spec,
          self._action_spec,
          tf.train.AdamOptimizer(),
          actor_net=DummyActorNet(self._action_spec,),
          value_net=DummyValueNet(),
          debug_summaries=True,
      )
      observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
      time_steps = ts.restart(observations, batch_size=2)
      actions = tf.constant([[0], [1]], dtype=tf.float32)
      returns = tf.constant([1.9, 1.0], dtype=tf.float32)
      sample_action_log_probs = tf.constant([0.9, 0.3], dtype=tf.float32)
      advantages = tf.constant([1.9, 1.0], dtype=tf.float32)
      valid_mask = tf.ones_like(advantages)
      sample_action_distribution_parameters = {
          'loc': tf.constant([[9.0], [15.0]], dtype=tf.float32),
          'scale': tf.constant([[8.0], [12.0]], dtype=tf.float32),
      }
      train_step = tf.train.get_or_create_global_step()

      with self.test_session() as sess:
        tf.contrib.summary.initialize(session=sess)

        (_, _) = (
            agent.build_train_op(
                time_steps, actions, sample_action_log_probs, returns,
                advantages, sample_action_distribution_parameters, valid_mask,
                train_step, summarize_gradients=False,
                gradient_clipping=0.0, debug_summaries=False))
        summaries_without_debug = tf.contrib.summary.all_summary_ops()

        (_, _) = (
            agent.build_train_op(
                time_steps, actions, sample_action_log_probs, returns,
                advantages, sample_action_distribution_parameters, valid_mask,
                train_step, summarize_gradients=False,
                gradient_clipping=0.0, debug_summaries=True))
        summaries_with_debug = tf.contrib.summary.all_summary_ops()

        self.assertGreater(
            len(summaries_with_debug), len(summaries_without_debug))

  @parameterized.named_parameters([
      ('IsZero', 0),
      ('NotZero', 1),
  ])
  def testL2RegularizationLoss(self, not_zero):
    l2_reg = 1e-4 * not_zero
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec),
        value_net=DummyValueNet(),
        normalize_observations=False,
        policy_l2_reg=l2_reg,
        value_function_l2_reg=l2_reg,
    )

    # Call other loss functions to make sure trainable variables are
    #   constructed.
    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([[0], [1]], dtype=tf.float32)
    returns = tf.constant([1.9, 1.0], dtype=tf.float32)
    sample_action_log_probs = tf.constant([[0.9], [0.3]], dtype=tf.float32)
    advantages = tf.constant([1.9, 1.0], dtype=tf.float32)
    current_policy_distribution, unused_network_state = DummyActorNet(
        self._action_spec)(time_steps.observation, time_steps.step_type, ())
    valid_mask = tf.ones_like(advantages)
    agent.policy_gradient_loss(time_steps, actions, sample_action_log_probs,
                               advantages, current_policy_distribution,
                               valid_mask)
    agent.value_estimation_loss(time_steps, returns, valid_mask)

    # Now request L2 regularization loss.
    # Value function weights are [2, 1], actor net weights are [2, 1, 1, 1].
    expected_loss = l2_reg * ((2**2 + 1) + (2**2 + 1 + 1 + 1))
    loss = agent.l2_regularization_loss()

    self.evaluate(tf.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  @parameterized.named_parameters([
      ('IsZero', 0),
      ('NotZero', 1),
  ])
  def testEntropyRegularizationLoss(self, not_zero):
    ent_reg = 0.1 * not_zero
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec),
        value_net=DummyValueNet(),
        normalize_observations=False,
        entropy_regularization=ent_reg,
    )

    # Call other loss functions to make sure trainable variables are
    #   constructed.
    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([[0], [1]], dtype=tf.float32)
    returns = tf.constant([1.9, 1.0], dtype=tf.float32)
    sample_action_log_probs = tf.constant([[0.9], [0.3]], dtype=tf.float32)
    advantages = tf.constant([1.9, 1.0], dtype=tf.float32)
    valid_mask = tf.ones_like(advantages)
    current_policy_distribution, unused_network_state = DummyActorNet(
        self._action_spec)(time_steps.observation, time_steps.step_type, ())
    agent.policy_gradient_loss(time_steps, actions, sample_action_log_probs,
                               advantages, current_policy_distribution,
                               valid_mask)
    agent.value_estimation_loss(time_steps, returns, valid_mask)

    # Now request entropy regularization loss.
    # Action stdevs should be ~1.0, and mean entropy ~3.70111.
    expected_loss = -3.70111 * ent_reg
    loss = agent.entropy_regularization_loss(
        time_steps, current_policy_distribution, valid_mask)

    self.evaluate(tf.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testValueEstimationLoss(self):
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec),
        value_net=DummyValueNet(),
        value_pred_loss_coef=1.0,
        normalize_observations=False,
    )

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    returns = tf.constant([1.9, 1.0], dtype=tf.float32)
    valid_mask = tf.ones_like(returns)

    expected_loss = 123.205
    loss = agent.value_estimation_loss(time_steps, returns, valid_mask)

    self.evaluate(tf.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testPolicyGradientLoss(self):
    actor_net = DummyActorNet(self._action_spec)
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        normalize_observations=False,
        normalize_rewards=False,
        actor_net=actor_net,
        importance_ratio_clipping=10.0,
    )

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([[0], [1]], dtype=tf.float32)
    sample_action_log_probs = tf.constant([0.9, 0.3], dtype=tf.float32)
    advantages = tf.constant([1.9, 1.0], dtype=tf.float32)
    valid_mask = tf.ones_like(advantages)

    current_policy_distribution, unused_network_state = actor_net(
        time_steps.observation, time_steps.step_type, ())

    expected_loss = -0.0164646133
    loss = agent.policy_gradient_loss(time_steps, actions,
                                      sample_action_log_probs, advantages,
                                      current_policy_distribution, valid_mask)

    self.evaluate(tf.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testKlPenaltyLoss(self):
    actor_net = actor_distribution_network.ActorDistributionNetwork(
        self._time_step_spec.observation,
        self._action_spec,
        fc_layer_params=None)
    value_net = value_network.ValueNetwork(
        self._time_step_spec.observation, fc_layer_params=None)
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=actor_net,
        value_net=value_net,
        kl_cutoff_factor=5.0,
        adaptive_kl_target=0.1,
        kl_cutoff_coef=100,
    )

    agent.kl_cutoff_loss = mock.MagicMock(
        return_value=tf.constant(3.0, dtype=tf.float32))
    agent.adaptive_kl_loss = mock.MagicMock(
        return_value=tf.constant(4.0, dtype=tf.float32))

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    action_distribution_parameters = {
        'loc': tf.constant([1.0, 1.0], dtype=tf.float32),
        'scale': tf.constant([1.0, 1.0], dtype=tf.float32),
    }
    current_policy_distribution, unused_network_state = DummyActorNet(
        self._action_spec)(time_steps.observation, time_steps.step_type, ())
    valid_mask = tf.ones_like(time_steps.discount)

    expected_kl_penalty_loss = 7.0

    kl_penalty_loss = agent.kl_penalty_loss(
        time_steps, action_distribution_parameters, current_policy_distribution,
        valid_mask)
    self.evaluate(tf.global_variables_initializer())
    kl_penalty_loss_ = self.evaluate(kl_penalty_loss)
    self.assertEqual(expected_kl_penalty_loss, kl_penalty_loss_)

  @parameterized.named_parameters([
      ('IsZero', 0),
      ('NotZero', 1),
  ])
  def testKlCutoffLoss(self, not_zero):
    kl_cutoff_coef = 30.0 * not_zero
    actor_net = actor_distribution_network.ActorDistributionNetwork(
        self._time_step_spec.observation,
        self._action_spec,
        fc_layer_params=None)
    value_net = value_network.ValueNetwork(
        self._time_step_spec.observation, fc_layer_params=None)
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=actor_net,
        value_net=value_net,
        kl_cutoff_factor=5.0,
        adaptive_kl_target=0.1,
        kl_cutoff_coef=kl_cutoff_coef,
    )
    kl_divergence = tf.constant([[1.5, -0.5, 6.5, -1.5, -2.3]],
                                dtype=tf.float32)
    expected_kl_cutoff_loss = kl_cutoff_coef * (.24**2)  # (0.74 - 0.5) ^ 2

    loss = agent.kl_cutoff_loss(kl_divergence)
    self.evaluate(tf.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose([loss_], [expected_kl_cutoff_loss])

  def testAdaptiveKlLoss(self):
    actor_net = actor_distribution_network.ActorDistributionNetwork(
        self._time_step_spec.observation,
        self._action_spec,
        fc_layer_params=None)
    value_net = value_network.ValueNetwork(
        self._time_step_spec.observation, fc_layer_params=None)
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=actor_net,
        value_net=value_net,
        initial_adaptive_kl_beta=1.0,
        adaptive_kl_target=10.0,
        adaptive_kl_tolerance=0.5,
    )
    kl_divergence = tf.placeholder(shape=[1], dtype=tf.float32)
    loss = agent.adaptive_kl_loss(kl_divergence)
    update = agent.update_adaptive_kl_beta(kl_divergence)

    with self.test_session() as sess:
      sess.run(tf.global_variables_initializer())

      # Loss should not change if data kl is target kl.
      loss_1 = sess.run(loss, feed_dict={kl_divergence: [10.0]})
      loss_2 = sess.run(loss, feed_dict={kl_divergence: [10.0]})
      self.assertEqual(loss_1, loss_2)

      # If data kl is low, kl penalty should decrease between calls.
      loss_1 = sess.run(loss, feed_dict={kl_divergence: [1.0]})
      sess.run(update, feed_dict={kl_divergence: [1.0]})
      loss_2 = sess.run(loss, feed_dict={kl_divergence: [1.0]})
      self.assertGreater(loss_1, loss_2)

      # If data kl is low, kl penalty should increase between calls.
      loss_1 = sess.run(loss, feed_dict={kl_divergence: [100.0]})
      sess.run(update, feed_dict={kl_divergence: [100.0]})
      loss_2 = sess.run(loss, feed_dict={kl_divergence: [100.0]})
      self.assertLess(loss_1, loss_2)

  def testUpdateAdaptiveKlBeta(self):
    actor_net = actor_distribution_network.ActorDistributionNetwork(
        self._time_step_spec.observation,
        self._action_spec,
        fc_layer_params=None)
    value_net = value_network.ValueNetwork(
        self._time_step_spec.observation, fc_layer_params=None)
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=actor_net,
        value_net=value_net,
        initial_adaptive_kl_beta=1.0,
        adaptive_kl_target=10.0,
        adaptive_kl_tolerance=0.5,
    )
    kl_divergence = tf.placeholder(shape=[1], dtype=tf.float32)
    updated_adaptive_kl_beta = agent.update_adaptive_kl_beta(kl_divergence)

    with self.test_session() as sess:
      sess.run(tf.global_variables_initializer())

      # When KL is target kl, beta should not change.
      beta_0 = sess.run(
          updated_adaptive_kl_beta, feed_dict={kl_divergence: [10.0]})
      expected_beta_0 = 1.0
      self.assertEqual(expected_beta_0, beta_0)

      # When KL is large, beta should increase.
      beta_1 = sess.run(
          updated_adaptive_kl_beta, feed_dict={kl_divergence: [100.0]})
      expected_beta_1 = 1.5
      self.assertEqual(expected_beta_1, beta_1)

      # When KL is small, beta should decrease.
      beta_2 = sess.run(
          updated_adaptive_kl_beta, feed_dict={kl_divergence: [1.0]})
      expected_beta_2 = 1.0
      self.assertEqual(expected_beta_2, beta_2)

  def testPolicy(self):
    value_net = value_network.ValueNetwork(
        self._time_step_spec.observation, fc_layer_params=None)
    agent = ppo_agent.PPOAgent(
        self._time_step_spec,
        self._action_spec,
        tf.train.AdamOptimizer(),
        actor_net=DummyActorNet(self._action_spec),
        value_net=value_net)
    observations = tf.constant([1, 2], dtype=tf.float32)
    time_steps = ts.restart(observations)
    action_step = agent.policy().action(time_steps)
    actions = action_step.action
    self.assertEqual(actions.shape.as_list(), [1])
    self.evaluate(tf.global_variables_initializer())
    _ = self.evaluate(actions)

  def testNormalizeAdvantages(self):
    advantages = np.array([1.1, 3.2, -1.5, 10.9, 5.6])
    mean = np.sum(advantages) / float(len(advantages))
    variance = np.sum(np.square(advantages - mean)) / float(len(advantages))
    stdev = np.sqrt(variance)
    expected_advantages = (advantages - mean) / stdev
    normalized_advantages = ppo_agent._normalize_advantages(
        tf.constant(advantages, dtype=tf.float32), variance_epsilon=0.0)
    self.assertAllClose(expected_advantages,
                        self.evaluate(normalized_advantages))


if __name__ == '__main__':
  tf.test.main()
