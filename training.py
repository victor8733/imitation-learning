import torch
from torch.nn import functional as F
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader, Dataset


# Dataset that returns transition tuples of the form (s, a, r, s', terminal)
class TransitionDataset(Dataset):
  def __init__(self, transitions):
    super().__init__()
    self.states, self.actions, self.rewards, self.terminals = transitions['states'], transitions['actions'].detach(), transitions['rewards'], transitions['terminals']

  def __getitem__(self, idx):
    return self.states[idx], self.actions[idx], self.rewards[idx], self.states[idx + 1], self.terminals[idx]

  def __len__(self):
    return self.terminals.size(0) - 1  # Need to return state and next state


# Computes and stores generalised advantage estimates ψ in the set of trajectories
def compute_advantages(trajectories, discount, trace_decay):
  with torch.no_grad():  # Do not differentiate through advantage calculation
    reward_to_go, advantage, next_value = torch.tensor([0.]), torch.tensor([0.]), torch.tensor([0.])
    for transition in reversed(trajectories):
      reward_to_go = transition['rewards'] + (1 - transition['terminals']) * (discount * reward_to_go)  # Reward-to-go/value R
      transition['rewards_to_go'] = reward_to_go
      td_error = transition['rewards'] + (1 - transition['terminals']) * discount * next_value - transition['values']  # TD-error δ
      advantage = td_error + (1 - transition['terminals']) * discount * trace_decay * advantage  # Generalised advantage estimate ψ
      transition['advantages'] = advantage
      next_value = transition['values']


# Performs one PPO update (assumes trajectories for first epoch are attached to agent)
def ppo_update(agent, trajectories, agent_optimiser, ppo_clip, epoch, value_loss_coeff=1, entropy_loss_coeff=1):
  # Recalculate outputs for subsequent iterations
  if epoch > 0:
    policy, trajectories['values'] = agent(trajectories['states'])
    trajectories['log_prob_actions'], trajectories['entropies'] = policy.log_prob(trajectories['actions'].detach()), policy.entropy()

  policy_ratio = (trajectories['log_prob_actions'] - trajectories['old_log_prob_actions']).exp()
  policy_loss = -torch.min(policy_ratio * trajectories['advantages'], torch.clamp(policy_ratio, min=1 - ppo_clip, max=1 + ppo_clip) * trajectories['advantages']).mean()  # Update the policy by maximising the clipped PPO objective
  value_loss = F.mse_loss(trajectories['values'], trajectories['rewards_to_go'])  # Fit value function by regression on mean squared error
  entropy_loss = -trajectories['entropies'].mean()  # Add entropy regularisation
  
  agent_optimiser.zero_grad()
  (policy_loss + value_loss_coeff * value_loss + entropy_loss_coeff * entropy_loss).backward()
  clip_grad_norm_(agent.parameters(), 1)  # Clamp norm of gradients
  agent_optimiser.step()


# Performs a behavioural cloning update
def behavioural_cloning_update(agent, expert_trajectories, agent_optimiser, batch_size):
  expert_dataloader = DataLoader(expert_trajectories, batch_size=batch_size, shuffle=True, drop_last=True)

  for expert_transition in expert_dataloader:
    expert_state, expert_action = expert_transition[0], expert_transition[1]

    agent_optimiser.zero_grad()
    behavioural_cloning_loss = -agent.log_prob(expert_state, expert_action).mean()  # Maximum likelihood objective
    behavioural_cloning_loss.backward()
    agent_optimiser.step()


# Performs an adversarial imitation learning update
def adversarial_imitation_update(algorithm, agent, discriminator, expert_trajectories, policy_trajectories, discriminator_optimiser, batch_size):
  expert_dataloader = DataLoader(expert_trajectories, batch_size=batch_size, shuffle=True, drop_last=True)
  policy_dataloader = DataLoader(policy_trajectories, batch_size=batch_size, shuffle=True, drop_last=True)

  # Iterate over mininum of expert and policy data
  for expert_transition, policy_transition in zip(expert_dataloader, policy_dataloader):
    expert_state, expert_action, expert_next_state, policy_state, policy_action, policy_next_state = expert_transition[0], expert_transition[1], expert_transition[3], policy_transition[0], policy_transition[1], policy_transition[3]

    if algorithm == 'GAIL':
      D_expert = discriminator(expert_state, expert_action)
      D_policy = discriminator(policy_state, policy_action)
    elif algorithm == 'AIRL':
      with torch.no_grad():
        expert_data_policy = agent.log_prob(expert_state, expert_action).exp()
        policy_data_policy = agent.log_prob(policy_state, policy_action).exp()
      D_expert = discriminator(expert_state, expert_action, expert_next_state, expert_data_policy)
      D_policy = discriminator(policy_state, expert_action, policy_next_state, policy_data_policy)
 
    # Binary logistic regression
    discriminator_optimiser.zero_grad()
    expert_loss = F.binary_cross_entropy(D_expert, torch.ones_like(D_expert))  # Loss on "real" (expert) data
    policy_loss = F.binary_cross_entropy(D_policy, torch.zeros_like(D_policy))  # Loss on "fake" (policy) data
    (expert_loss + policy_loss).backward()
    discriminator_optimiser.step()
