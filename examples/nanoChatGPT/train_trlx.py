import os
from typing import List

import torch
from datasets import load_dataset
from models.reward import RewardModel
from tqdm import tqdm
from transformers import AutoTokenizer

import trlx
from trlx.data.configs import (
    ModelConfig,
    OptimizerConfig,
    SchedulerConfig,
    TokenizerConfig,
    TrainConfig,
    TRLConfig,
)
from trlx.models.modeling_ppo import PPOConfig
from utils import load_and_update_config

SFT_MODEL_PATH = "out"

config = TRLConfig(
    train=TrainConfig(
        seq_length=550,
        epochs=50,
        total_steps=100_000,
        batch_size=4,
        checkpoint_interval=10_000,
        eval_interval=200,
        pipeline="PromptPipeline",
        trainer="AcceleratePPOTrainer",
        tracker=None,
    ),
    model=ModelConfig(
        model_path="out",
        num_layers_unfrozen=8,
    ),
    tokenizer=TokenizerConfig(
        tokenizer_path="gpt2",
        truncation_side="right",
    ),
    optimizer=OptimizerConfig(
        name="adamw",
        kwargs={
            "lr": 5.0e-6,
            "betas": [0.9, 0.999],
            "eps": 1.0e-8,
            "weight_decay": 0.01,
        },
    ),
    scheduler=SchedulerConfig(
        name="cosine_annealing",
        kwargs={
            "T_max": 100000,
            "eta_min": 5.0e-6,
        },
    ),
    method=PPOConfig(
        name="PPOConfig",
        num_rollouts=128,
        chunk_size=16,
        ppo_epochs=4,
        init_kl_coef=0.1,
        target=6,
        horizon=10000,
        gamma=1,
        lam=0.95,
        cliprange=0.2,
        cliprange_value=0.2,
        vf_coef=0.2,
        scale_reward=None,
        ref_mean=None,
        ref_std=None,
        cliprange_reward=10,
        gen_kwargs={
            "max_new_tokens": 50,
        },
    ),
)

class Counter():
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return ""


def main():
    MISSING_KEYS_COUNTER = Counter()
    # Load the pre-trained reward model
    rw_tokenizer = AutoTokenizer.from_pretrained("gpt2")
    rw_tokenizer.pad_token = rw_tokenizer.eos_token
    rw_model = RewardModel.from_pretrained("out_reward")
    # rw_model.half()
    rw_model.eval()
    rw_device = torch.device("cuda:{}".format(1))  # set reward model device
    rw_model.to(rw_device)

    def get_scores(samples: List[str]):
        scores_list = []
        batch_size = 2
        for i in range(0, len(samples), batch_size):
            sub_samples = samples[i : i + batch_size]
            sub_samples = ["<|startoftext|>" + chosen + "<|endoftext|>" for chosen in sub_samples]
            encodings_dict = rw_tokenizer(
                sub_samples,
                truncation=True,
                max_length=config.train.seq_length,
                padding="max_length",
                return_tensors="pt",
            )
            input_ids = encodings_dict["input_ids"].to(rw_device)
            attn_masks = encodings_dict["attention_mask"].to(rw_device)
            with torch.no_grad():
                _, end_scores = rw_model(input_ids=input_ids, attention_mask=attn_masks)
            scores_list.append(end_scores)
        scores = torch.cat(scores_list, dim=0)
        return scores

    def get_prompt_dataset(prompts, max_length):
        """
        Get the prompt after T5 decoding to make sure dictionary
        of prompts and summaries is consistent decode prompt from trlX pipeline
        """
        formatted_prompts = []
        for i in tqdm(range(len(prompts))):
            tmp = tokenizer.decode(
                tokenizer(
                    prompts[i].split("TL;DR:")[0],
                    truncation=True,
                    max_length=max_length - 5,  # to make sure "TL;DR" dont get truncated
                    add_special_tokens=False,
                )["input_ids"],
                skip_special_tokens=True,
            ).strip()
            tmp = tmp + "\nTL;DR:"
            tmp = tokenizer.decode(
                tokenizer(tmp, truncation=True, max_length=max_length, add_special_tokens=False)["input_ids"],
                skip_special_tokens=True,
            ).strip()
            formatted_prompts.append(tmp)
            if i >= 1000:
                break
        return formatted_prompts

    def reward_fn(samples: List[str], **kwargs):
        import ipdb; ipdb.set_trace()
        original_samples = [text.split("TL;DR:")[0] + "TL;DR: " for text in samples]
        original_samples = [text + (post_summary_dict.get(text.strip()) or MISSING_KEYS_COUNTER()) for text in original_samples]
        original_scores = get_scores(original_samples)
        scores = get_scores(samples)
        norms_scores = scores - original_scores
        if MISSING_KEYS_COUNTER.count:
            print("missing keys:", MISSING_KEYS_COUNTER.count)
        return norms_scores

    tokenizer = AutoTokenizer.from_pretrained("gpt2")
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    max_length_input = config.train.seq_length - config.method.gen_kwargs["max_new_tokens"]

    dataset = load_dataset("CarperAI/openai_summarize_tldr")

    # Store data into prompt and label pairs
    train_set = [(sample["prompt"], sample["label"]) for sample in dataset["train"]]
    val_set = [(sample["prompt"], sample["label"]) for sample in dataset["valid"]]

    # Split contents into summaries and labels
    train_posts, train_summaries = zip(*train_set)
    val_posts, val_summaries = zip(*val_set)

    # Get the OpenAI summaries
    post_summary_dict = {}
    train_prompts = get_prompt_dataset(train_posts, max_length_input)
    for i in range(len(train_prompts)):
        post_summary_dict[train_prompts[i]] = train_summaries[i]
    val_prompts = get_prompt_dataset(val_posts, max_length_input)
    for i in range(len(val_prompts)):
        post_summary_dict[val_prompts[i]] = val_summaries[i]

    trainer = trlx.train(
        reward_fn=reward_fn,
        prompts=train_prompts,
        eval_prompts=val_prompts[0:1000],  # sampling 1000 validation prompts for evaluation speed in training
        config=config,
    )


if __name__ == "__main__":
    main()
