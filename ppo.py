import time

import torch
import numpy as np
import spinup.algos.pytorch.ppo.core as core
import matplotlib.pyplot as plt

from spinup.algos.pytorch.ppo.ppo import PPOBuffer
from spinup.utils.logx import EpochLogger
from spinup.utils.mpi_pytorch import setup_pytorch_for_mpi, sync_params, mpi_avg_grads
from spinup.utils.mpi_tools import mpi_avg, proc_id, num_procs
from torch.optim import Adam


def run_policy(env, ac, max_ep_len=None, num_episodes=10):
    returns = []
    ep_lenghts = []

    o, r, d, ep_ret, ep_len, n = env.reset(), 0, False, 0, 0, 0
    while n < num_episodes:
        a, v, logp = ac.step(torch.as_tensor(o, dtype=torch.float32))
        o, r, d, _ = env.step(a)
        ep_ret += r
        ep_len += 1

        if d or (ep_len == max_ep_len):
            returns.append(ep_ret)
            ep_lenghts.append(ep_len)
            o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0
            n += 1

    return returns, ep_lenghts


def generate_train_graph(train_returns, save_path):
    # Plot the change in the validation and training set error over training.
    fig = plt.figure(figsize=(8, 4))
    ax_1 = fig.add_subplot(111)

    ax_1.plot(train_returns, label="Training returns")
    # ax_1.plot(range(test_frequency, len(test_returns)*test_frequency + 1, test_frequency), test_returns, label="Test returns")
    ax_1.legend(loc=0)
    ax_1.set_xlabel('Epochs')
    ax_1.set_ylabel('Average returns')
    fig.suptitle('Returns')
    fig.savefig(save_path)
    plt.clf()
    plt.cla()
    plt.close(fig)


class EpochLoggerFixed(EpochLogger):
    def log_tabular(self, key, val=None, with_min_and_max=False, average_only=False):
        try:
            super().log_tabular(key, val, with_min_and_max, average_only)
        except:
            pass


class RefactoredPPO:
    def __init__(self, env_fn, actor_critic=core.MLPActorCritic, ac_kwargs=dict(), seed=0,
                 steps_per_epoch=4000, epochs=50, gamma=0.99, clip_ratio=0.2, pi_lr=3e-4,
                 vf_lr=1e-3, train_pi_iters=80, train_v_iters=80, lam=0.97, max_ep_len=1000,
                 target_kl=0.01, logger_kwargs=None, save_freq=10, train_graph_path='/home/visgean/',
                 train_graph_name='return.svg', model=None):

        self.actor_critic = actor_critic
        self.ac_kwargs = ac_kwargs
        self.seed = seed
        self.steps_per_epoch = steps_per_epoch
        self.epochs = epochs
        self.gamma = gamma
        self.clip_ratio = clip_ratio
        self.pi_lr = pi_lr
        self.vf_lr = vf_lr
        self.train_pi_iters = train_pi_iters
        self.train_v_iters = train_v_iters
        self.lam = lam
        self.max_ep_len = max_ep_len
        self.target_kl = target_kl
        self.logger_kwargs = logger_kwargs if logger_kwargs else {}
        self.save_freq = save_freq

        # Special function to avoid certain slowdowns from PyTorch + MPI combo.
        setup_pytorch_for_mpi()

        # Set up logger and save configuration
        self.logger = EpochLoggerFixed(**self.logger_kwargs)
        self.logger.save_config(locals())

        # Random seed
        self.seed += 10000 * proc_id()
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)

        # Instantiate environment
        self.env = env_fn()
        self.obs_dim = self.env.observation_space.shape
        self.act_dim = self.env.action_space.shape

        # Create actor-critic module
        if model:
            self.ac = model
        else:
            self.ac = actor_critic(self.env.observation_space, self.env.action_space, **ac_kwargs)

        # Sync params across processes
        sync_params(self.ac)

        # Count variables
        self.var_counts = tuple(core.count_vars(module) for module in [self.ac.pi, self.ac.v])
        self.logger.log('\nNumber of parameters: \t pi: %d, \t v: %d\n' % self.var_counts)

        # Set up experience buffer
        self.local_steps_per_epoch = int(steps_per_epoch / num_procs())
        self.buf = PPOBuffer(self.obs_dim, self.act_dim, self.local_steps_per_epoch, gamma, lam)

        # Set up optimizers for policy and value function
        self.pi_optimizer = Adam(self.ac.pi.parameters(), lr=pi_lr)
        self.vf_optimizer = Adam(self.ac.v.parameters(), lr=vf_lr)

        # Set up model saving
        self.logger.setup_pytorch_saver(self.ac)

        # Prepare for interaction with environment
        self.start_time = time.time()
        self.obs = self.env.reset()
        self.ep_ret = 0
        self.ep_len = 0

        self.test_returns = []
        self.train_returns = []
        self.max_return = 0

        self.test_lengths = []

        self.train_graph_path = train_graph_path + f'{proc_id()}_{train_graph_name}'

    def train_epoch(self):
        for t in range(self.local_steps_per_epoch):
            a, v, logp = self.ac.step(torch.as_tensor(self.obs, dtype=torch.float32))

            next_o, r, d, _ = self.env.step(a)
            self.ep_ret += r
            self.ep_len += 1

            # save and log
            self.buf.store(self.obs, a, r, v, logp)
            self.logger.store(VVals=v)

            # Update obs (critical!)
            self.obs = next_o

            timeout = self.ep_len == self.max_ep_len
            terminal = d or timeout
            epoch_ended = t == self.local_steps_per_epoch - 1

            if terminal or epoch_ended:
                if epoch_ended and not terminal:
                    print('Warning: trajectory cut off by epoch at %d steps.' % self.ep_len, flush=True)
                # if trajectory didn't reach terminal state, bootstrap value target
                if timeout or epoch_ended:
                    _, v, _ = self.ac.step(torch.as_tensor(self.obs, dtype=torch.float32))
                else:
                    v = 0
                self.buf.finish_path(v)
                if terminal:
                    # only save EpRet / EpLen if trajectory finished
                    self.logger.store(EpRet=self.ep_ret, EpLen=self.ep_len)
                self.obs = self.env.reset()

                self.ep_ret = 0
                self.ep_len = 0

    def train(self):
        # Main loop: collect experience in env and update/log each epoch
        for epoch in range(self.epochs):
            self.train_epoch()
            self.save_env(epoch)

            # Perform PPO update!
            self.update()

            # Log info about epoch
            self.logger.log_tabular('Epoch', epoch)
            self.logger.log_tabular('EpRet', with_min_and_max=True)
            self.logger.log_tabular('EpLen', average_only=True)
            self.logger.log_tabular('VVals', with_min_and_max=True)
            self.logger.log_tabular('TotalEnvInteracts', (epoch + 1) * self.steps_per_epoch)
            self.logger.log_tabular('LossPi', average_only=True)
            self.logger.log_tabular('LossV', average_only=True)
            self.logger.log_tabular('DeltaLossPi', average_only=True)
            self.logger.log_tabular('DeltaLossV', average_only=True)
            self.logger.log_tabular('Entropy', average_only=True)
            self.logger.log_tabular('KL', average_only=True)
            self.logger.log_tabular('ClipFrac', average_only=True)
            self.logger.log_tabular('StopIter', average_only=True)
            self.logger.log_tabular('Time', time.time() - self.start_time)
            self.logger.dump_tabular()

    def save_env(self, epoch):
        self.train_returns.append(np.mean(self.logger.epoch_dict['EpRet']))

        if epoch > 0 and epoch % self.save_freq == 0 and proc_id() == 0:
            # returns, lengths = run_policy(self.env, self.ac)
            # avg_return = np.mean(returns)

            # self.test_returns.append(avg_return)
            # self.test_lengths.append(np.mean(lengths))
            #
            # if avg_return > self.max_return:

            self.logger.save_state({'env': self.env}, epoch)
            generate_train_graph(self.train_returns, self.train_graph_path)

            # self.obs = self.env.reset()

        if epoch == self.epochs - 1 and proc_id() == 0:
            self.logger.save_state({'env': self.env}, epoch)

    def compute_loss_v(self, data):
        obs, ret = data['obs'], data['ret']
        return ((self.ac.v(obs) - ret) ** 2).mean()

    def update(self):
        data = self.buf.get()

        pi_l_old, pi_info_old = self.compute_loss_pi(data)
        pi_l_old = pi_l_old.item()
        v_l_old = self.compute_loss_v(data).item()

        # Train policy with multiple steps of gradient descent
        for i in range(self.train_pi_iters):
            self.pi_optimizer.zero_grad()
            loss_pi, pi_info = self.compute_loss_pi(data)
            kl = mpi_avg(pi_info['kl'])
            if kl > 1.5 * self.target_kl:
                self.logger.log('Early stopping at step %d due to reaching max kl.' % i)
                break
            loss_pi.backward()
            mpi_avg_grads(self.ac.pi)  # average grads across MPI processes
            self.pi_optimizer.step()

        self.logger.store(StopIter=i)

        # Value function learning
        for i in range(self.train_v_iters):
            self.vf_optimizer.zero_grad()
            loss_v = self.compute_loss_v(data)
            loss_v.backward()
            mpi_avg_grads(self.ac.v)  # average grads across MPI processes
            self.vf_optimizer.step()

        # Log changes from update
        kl, ent, cf = pi_info['kl'], pi_info_old['ent'], pi_info['cf']
        try:
            self.logger.store(LossPi=pi_l_old, LossV=v_l_old,
                              KL=kl, Entropy=ent, ClipFrac=cf,
                              DeltaLossPi=(loss_pi.item() - pi_l_old),
                              DeltaLossV=(loss_v.item() - v_l_old))
        except:
            pass

    # Set up function for computing PPO policy loss
    def compute_loss_pi(self, data):
        obs, act, adv, logp_old = data['obs'], data['act'], data['adv'], data['logp']

        # Policy loss
        pi, logp = self.ac.pi(obs, act)
        ratio = torch.exp(logp - logp_old)
        clip_adv = torch.clamp(ratio, 1 - self.clip_ratio, 1 + self.clip_ratio) * adv
        loss_pi = -(torch.min(ratio * adv, clip_adv)).mean()

        # Useful extra info
        approx_kl = (logp_old - logp).mean().item()
        ent = pi.entropy().mean().item()
        clipped = ratio.gt(1 + self.clip_ratio) | ratio.lt(1 - self.clip_ratio)
        clipfrac = torch.as_tensor(clipped, dtype=torch.float32).mean().item()
        pi_info = dict(kl=approx_kl, ent=ent, cf=clipfrac)

        return loss_pi, pi_info
