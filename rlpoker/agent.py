import tensorflow as tf
from collections import deque
import random
import numpy as np

from rlpoker.best_response import compute_exploitability

class Reservoir:
    def __init__(self, maxlen):
        self.maxlen = maxlen
        self.i = 0
        self.reservoir = deque()

    def append(self, item):
        """Implements reservoir sampling.

        Let the item be the ith item. If i < self.maxlen, then we keep the item. Otherwise, we keep the new item with
        probability self.maxlen / i and otherwise discard it. If we keep the new item, we randomly choose an old item
        to discard.
        """
        self.i += 1
        if len(self.reservoir) < self.maxlen:
            self.reservoir.append(item)
        else:
            # With probability self.maxlen / i, replace an existing item with the new item.
            if np.random.rand() < self.maxlen / self.i:
                discard_idx = np.random.choice(self.maxlen)
                self.reservoir[discard_idx] = item

    def sample(self, n):
        """Samples n items randomly from the reservoir.
        """
        return random.sample(self.reservoir, n)

    def __len__(self):
        return len(self.reservoir)


class CircularBuffer:
    """Implements a circular buffer with maximum length.
    """

    def __init__(self, maxlen=None):
        self.maxlen = maxlen
        self.buffer = deque(maxlen=maxlen)

    def append(self, item):
        """Appends an item to the buffer.
        """
        self.buffer.append(item)

    def sample(self, n):
        """Samples n items randomly from the buffer.
        """
        return random.sample(self.buffer, n)

    def __len__(self):
        return len(self.buffer)


class Agent:
    def __init__(self, name, input_dim, action_dim, max_replay=200000,
                 max_supervised=2000000, best_response_lr=1e-4,
                 supervised_lr=1e-5):
        # Replay memory is a circular buffer, and supervised learning memory is a reservoir.
        self.replay_memory = CircularBuffer(max_replay)
        self.supervised_memory = Reservoir(max_supervised)

        self.input_dim = input_dim
        self.action_dim = action_dim

        self.name = name
        with tf.variable_scope('agent_{}'.format(name)):
            self.q_network = self.create_q_network('current_q', input_dim, action_dim)
            self.target_q_network = self.create_q_network('target_q', input_dim, action_dim)
            self.policy_network = self.create_policy_network('policy', input_dim, action_dim)

            # Create ops for copying current network to target network. We create a list
            # of the variables in both networks and then create an assign operation that
            # copies the value in the current variable to the corresponding target variable.
            current_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='current_q')
            target_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='target_q')
            self.update_ops = [t.assign(c) for t, c in zip(target_vars, current_vars)]

            # Set up Q-learning loss functions
            self.reward = tf.placeholder('float32', shape=[None])
            self.action = tf.placeholder('int32', shape=[None])
            one_hot_action = tf.one_hot(self.action, action_dim)

            q_value = tf.reduce_sum(one_hot_action * self.q_network['output'], axis=1)
            self.not_terminals = tf.placeholder('float32', shape=[None])
            next_q = self.reward + self.not_terminals * tf.reduce_max(tf.stop_gradient(self.target_q_network['output']),
                                                                      axis=1)
            self.q_loss = tf.reduce_mean(tf.square(next_q - q_value))
            self.q_trainer = tf.train.GradientDescentOptimizer(best_response_lr).minimize(self.q_loss)

            policy_for_actions = tf.reduce_sum(self.policy_network['output'] * one_hot_action, axis=1)
            self.policy_loss = tf.reduce_mean(-tf.log(policy_for_actions))
            self.policy_trainer = tf.train.GradientDescentOptimizer(supervised_lr).minimize(self.policy_loss)

    def append_replay_memory(self, transitions):
        for transition in transitions:
            self.replay_memory.append(transition)

    def append_supervised_memory(self, state_action_pairs):
        for state_action_pair in state_action_pairs:
            self.supervised_memory.append(state_action_pair)

    # Get the output of the q network for the given state
    def predict_q(self, sess, state):
        assert len(state.shape) == 2
        assert state.shape[1] == self.input_dim
        return sess.run(self.q_network['output'], feed_dict={
            self.q_network['input']: state
        })

    # Get the output of the q network for the given state
    def predict_policy(self, sess, state):
        assert len(state.shape) == 2
        assert state.shape[1] == self.input_dim
        return sess.run(self.policy_network['output'], feed_dict={
            self.policy_network['input']: state
        })

    def update_target_network(self, sess):
        # Copy current q_network parameters to target_q_network
        sess.run(self.update_ops)

    def train_q_network(self, sess, batch_size):
        # Sample a minibatch from the replay memory
        minibatch = self.replay_memory.sample(batch_size)

        states = np.array([d['state'] for d in minibatch])
        actions = np.array([d['action'] for d in minibatch])
        next_states = np.array([d['next_state'] for d in minibatch])
        rewards = np.array([d['reward'] for d in minibatch])
        terminals = np.array([d['terminal'] for d in minibatch])

        not_terminals = np.array([not t for t in terminals]).astype('float32')

        q_loss, _ = sess.run([self.q_loss, self.q_trainer], feed_dict={
            self.reward: rewards,
            self.action: actions,
            self.not_terminals: not_terminals,
            self.q_network['input']: states,
            self.target_q_network['input']: next_states
        })
        return q_loss

    def train_policy_network(self, sess, batch_size):
        # Sample a minibatch from the supervised memory
        minibatch = self.supervised_memory.sample(batch_size)

        states = np.array([d['state'] for d in minibatch])
        actions = np.array([d['action'] for d in minibatch])

        policy_loss, _ = sess.run([self.policy_loss, self.policy_trainer], feed_dict={
            self.policy_network['input']: states,
            self.action: actions
        })
        return policy_loss

    # Create a 2 layer neural network with relu activations on the hidden
    # layer. The output is the predicted q-value of an action.
    def create_q_network(self, scope, input_dim, action_dim, num_hidden=2, hidden_dim=64):
        with tf.variable_scope(scope):
            input_layer = tf.placeholder('float32', shape=[None, input_dim])

            hidden_layer = input_layer

            for i in range(num_hidden):
                hidden_layer = tf.layers.dense(hidden_layer, hidden_dim, activation=tf.nn.relu)

            output_layer = tf.layers.dense(hidden_layer, action_dim)
        return {'input': input_layer, 'output': output_layer}

    def create_policy_network(self, scope, input_dim, action_dim, num_hidden=2, hidden_dim=64):
        with tf.variable_scope(scope):
            input_layer = tf.placeholder('float32', shape=[None, input_dim])

            hidden_layer = input_layer
            for i in range(num_hidden):
                hidden_layer = tf.layers.dense(hidden_layer, hidden_dim, activation=tf.nn.relu)

            output_layer = tf.layers.dense(hidden_layer, action_dim, activation=tf.nn.softmax)
        return {'input': input_layer, 'output': output_layer}

    def get_strategy(self, sess, states):
        """Returns a strategy for an agent. This is a mapping from
        information sets in the game to probability distributions over
        actions.

        Args:
            sess: tensorflow session.
            states: dict. This is a dictionary with keys the information set
                ids and values the vectors to input to the network.
        """
        strategy = dict()
        for info_set_id, state in states.items():
            policy = self.predict_policy(sess, np.array([state])).ravel()
            strategy[info_set_id] = {i: policy[i] for i in range(len(policy))}

        return strategy

    def compute_exploitability(self, sess, game):
        """Computes the exploitability of the agent's current strategy.

        Args:
            game: ExtensiveGame.

        Returns:
            float. Exploitability of the agent's strategy.
        """
        states = game._state_vectors
        strategy = self.get_strategy(sess, states)

        return compute_exploitability(game._game, strategy)
