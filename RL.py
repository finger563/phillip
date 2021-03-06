import tensorflow as tf
import random
import ssbm
import ctypes
import tf_lib as tfl
import util
import ctype_util as ct
import numpy as np
import embed
from dqn import DQN
from actor_critic import ActorCritic
from actor_critic_split import ActorCriticSplit
from thompson_dqn import ThompsonDQN
from operator import add, sub
from enum import Enum
from reward import computeRewards

class Mode(Enum):
  TRAIN = 0
  PLAY = 1

models = {model.__name__ : model for model in [DQN, ActorCritic, ActorCriticSplit, ThompsonDQN]}

class RLConfig:
  def __init__(self, tdN=5, reward_halflife = 2.0, act_every=5, **kwargs):
    self.tdN = tdN
    self.reward_halflife = reward_halflife
    self.fps = 60 / act_every
    self.discount = 0.5 ** ( 1.0 / (self.fps*reward_halflife) )

class Model:
  def __init__(self,
              model="DQN",
              path=None,
              mode = Mode.TRAIN,
              debug = False,
              learning_rate=1e-4,
              gpu=False,
              optimizer="Adam",
              memory=0,
              **kwargs):
    print("Creating model:", model)
    modelType = models[model]
    
    self.path = path
    
    self.graph = tf.Graph()
    
    device = '/gpu:0' if gpu else '/cpu:0'
    print("Using device " + device)
    
    with self.graph.as_default(), tf.device(device):
      self.global_step = tf.Variable(0, name='global_step', trainable=False)

      self.rlConfig = RLConfig(**kwargs)
      
      embedGame = embed.GameEmbedding(**kwargs)
      state_size = embedGame.size
      
      history_size = (1+memory) * (state_size+embed.action_size)
      self.model = modelType(history_size, embed.action_size, self.global_step, self.rlConfig, **kwargs)

      #self.variables = self.model.getVariables() + [self.global_step]
      
      if mode == Mode.TRAIN:
        with tf.name_scope('train'):
          self.experience = ct.inputCType(ssbm.SimpleStateAction, [None, None], "experience")
          # instantaneous rewards for all but the first state
          self.experience['reward'] = tf.placeholder(tf.float32, [None, None], name='reward')
          
          states = embedGame(self.experience['state'])
          experience_length = tf.shape(states)[1]
          prev_actions = embed.embedAction(self.experience['prev_action'])
          states = tf.concat(2, [states, prev_actions])
          
          train_length = experience_length - memory
          
          history = [tf.slice(states, [0, i, 0], [-1, train_length, -1]) for i in range(memory+1)]
          self.train_states = tf.concat(2, history)
          
          actions = embed.embedAction(self.experience['action'])
          self.train_actions = tf.slice(actions, [0, memory, 0], [-1, train_length, -1])
          
          self.train_rewards = tf.slice(self.experience['reward'], [0, memory], [-1, -1])
          
          """
          data_names = ['state', 'action', 'reward']
          self.saved_data = [tf.get_session_handle(getattr(self, 'train_%ss' % name)) for name in data_names]
          
          self.placeholders = []
          loaded_data = []
          
          for name in data_names:
            placeholder, data = tf.get_session_tensor(tf.float32)
            self.placeholders.append(placeholder)
            #data = tf.reshape(data, tf.shape(getattr(self, 'embedded_%ss' % name)))
            loaded_data.append(data)
          
          loss, stats = self.model.getLoss(*loaded_data, **kwargs)
          """
          
          loss, stats = self.model.getLoss(self.train_states, self.train_actions, self.train_rewards, **kwargs)
          
          tf.scalar_summary("loss", loss)
          for name, tensor in stats:
            if tensor.dtype is tf.bool:
              tensor = tf.cast(tensor, tf.uint8)
            tf.scalar_summary(name, tensor)
          merged = tf.merge_all_summaries()
          
          self.optimizer = getattr(tf.train, optimizer + "Optimizer")(learning_rate)
          # train_q = opt.minimize(qLoss, global_step=global_step)
          # opt = tf.train.GradientDescentOptimizer(0.0)
          #grads_and_vars = opt.compute_gradients(qLoss)
          grads_and_vars = self.optimizer.compute_gradients(loss)
          self.grads_and_vars = [(g, v) for g, v in grads_and_vars if g is not None]
          self.train_op = self.optimizer.apply_gradients(grads_and_vars, global_step=self.global_step)
          self.run_dict = dict(summary=merged, global_step=self.global_step, train=self.train_op)
          
          self.writer = tf.train.SummaryWriter(path+'logs/', self.graph)
      else:
        with tf.name_scope('policy'):
          self.input = ct.inputCType(ssbm.SimpleStateAction, [memory+1], "input")
          states = embedGame(self.input['state'])
          prev_actions = embed.embedAction(self.input['prev_action'])
          
          history = tf.concat(1, [states, prev_actions])
          history = tf.reshape(history, [history_size])
          
          self.policy = self.model.getPolicy(history, **kwargs)
      
      tf_config = dict(
        allow_soft_placement=True
      )
      
      if mode == Mode.PLAY: # don't eat up cpu cores
        tf_config.update(
          inter_op_parallelism_threads=1,
          intra_op_parallelism_threads=1,
        )
      else:
        tf_config.update(
          #gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.3),
        )
      
      self.sess = tf.Session(
        graph=self.graph,
        config=tf.ConfigProto(**tf_config),
      )
      
      self.debug = debug
      
      #self.saver = tf.train.Saver(self.variables)
      self.saver = tf.train.Saver(tf.all_variables())

  def act(self, history, verbose=False):
    feed_dict = dict(util.deepValues(util.deepZip(self.input, ct.vectorizeCTypes(ssbm.SimpleStateAction, history))))
    return self.model.act(self.sess.run(self.policy, feed_dict), verbose)

  #summaryWriter = tf.train.SummaryWriter('logs/', sess.graph)
  #summaryWriter.flush()

  def debugGrads(self, feed_dict):
    gs = self.sess.run([gv[0] for gv in self.grads_and_vars], feed_dict)
    vs = self.sess.run([gv[1] for gv in self.grads_and_vars], feed_dict)
    #   loss = sess.run(qLoss, feed_dict)
    #act_qs = sess.run(qs, feed_dict)
    #act_qs = list(map(util.compose(np.sort, np.abs), act_qs))

    #t = sess.run(temperature)
    #print("Temperature: ", t)
    #for i, act in enumerate(act_qs):
    #  print("act_%d"%i, act)
    #print("grad/param(action)", np.mean(np.abs(gs[0] / vs[0])))
    #print("grad/param(stage)", np.mean(np.abs(gs[2] / vs[2])))

    print("param avg and max")
    for g, v in zip(gs, vs):
      abs_v = np.abs(v)
      abs_g = np.abs(g)
      print(v.shape, np.mean(abs_v), np.max(abs_v), np.mean(abs_g), np.max(abs_g))

    print("grad/param avg and max")
    for g, v in zip(gs, vs):
      ratios = np.abs(g / v)
      print(np.mean(ratios), np.max(ratios))
    #print("grad", np.mean(np.abs(gs[4])))
    #print("param", np.mean(np.abs(vs[0])))

    # if step_index == 10:
    import ipdb; ipdb.set_trace()

  def train(self, filenames, steps=1):
    #state_actions = ssbm.readStateActions(filename)
    #feed_dict = feedStateActions(state_actions)
    experiences = util.async_map(ssbm.readStateActions_pickle, filenames)()
    experiences = util.deepZip(*experiences)
    experiences = util.deepMap(np.array, experiences)
    
    input_dict = dict(util.deepValues(util.deepZip(self.experience, experiences)))
    
    """
    saved_data = self.sess.run(self.saved_data, input_dict)
    handles = [t.handle for t in saved_data]
    
    saved_dict = dict(zip(self.placeholders, handles))
    """

    if self.debug:
      self.debugGrads(input_dict)
    
    for _ in range(steps):
      results = tfl.run(self.sess, self.run_dict, input_dict)
      
      summary_str = results['summary']
      global_step = results['global_step']
      self.writer.add_summary(summary_str, global_step)

  def save(self):
    import os
    os.makedirs(self.path, exist_ok=True)
    print("Saving to", self.path)
    self.saver.save(self.sess, self.path + "snapshot")

  def restore(self):
    self.saver.restore(self.sess, self.path + "snapshot")

  def init(self):
    with self.graph.as_default():
      #self.sess.run(tf.initialize_variables(self.variables))
      self.sess.run(tf.initialize_all_variables())

