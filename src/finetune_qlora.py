"""
finetune_qlora.py -- Post-training for citation discipline (the mini "Harvey-Tenet").

REQUIRES A GPU MACHINE (not runnable on CPU-only hosts). Tested recipe:
  GPU: 1x RTX 4090 / A100 40GB.  Model: Qwen2.5-14B-Instruct (or Qwen3.x equiv).
  Method: QLoRA (4-bit NF4) + ORPO (single-pass SFT+alignment, no ref model).

What we fine-tune FOR (behavior, not knowledge):
  - every factual claim carries a citation like [chunk_dll]
  - exact refusal string when context is insufficient
  - short, structured answers (no rambling)
Knowledge stays in the RAG corpus; the weights only learn DISCIPLINE.
This is the correct division per the DeepSeek plan (SFT for format, ORPO for
preference alignment, verifiable rewards for citation/refusal/JSON validity).

Dataset: evals/finetune_pairs.jsonl  (built by scripts/make_finetune_data.py
using a frontier model to generate 500 (context -> ideal answer) pairs from
the rulebook chunks; human spot-check 10%).

Run on GPU host:
  pip install -r requirements-train.txt
  python3 src/finetune_qlora.py --model Qwen/Qwen2.5-14B-Instruct

requirements-train.txt: torch, transformers, peft, trl, datasets, bitsandbytes
"""

import argparse

PROMPT_TEMPLATE = """<system>{system}</system>
<context>
{context}
</context>
<question>{question}</question>
"""

SYSTEM = ("You are the Tradeify Risk Copilot. Answer ONLY from the context. "
          "Cite every factual claim like [chunk_dll]. If the context is "
          "insufficient, reply EXACTLY: "
          "\"I don't have that in the Tradeify rulebook I can access.\"")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-14B-Instruct")
    ap.add_argument("--data", default="evals/finetune_pairs.jsonl")
    ap.add_argument("--out", default="models/qwen14b-copilot-orpo")
    ap.add_argument("--epochs", type=int, default=2)
    args = ap.parse_args()

    import torch
    from datasets import load_dataset
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from peft import LoraConfig, prepare_model_for_kbit_training, get_peft_model
    from trl import ORPOConfig, ORPOTrainer

    print(f"loading {args.model} in 4-bit (needs GPU)...")
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                              bnb_4bit_compute_dtype=torch.bfloat16)
    model = AutoModelForCausalLM.from_pretrained(args.model, quantization_config=bnb,
                                                 device_map="auto")
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = prepare_model_for_kbit_training(model)
    lora = LoraConfig(r=32, lora_alpha=64, lora_dropout=0.05,
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj"],
                      task_type="CAUSAL_LM")
    model = get_peft_model(model, lora)

    ds = load_dataset("json", data_files=args.data)["train"]

    def fmt(ex):
        prompt = PROMPT_TEMPLATE.format(system=SYSTEM, context=ex["context"],
                                        question=ex["question"])
        return {"prompt": prompt, "chosen": ex["chosen"], "rejected": ex["rejected"]}

    ds = ds.map(fmt, remove_columns=ds.column_names)
    cfg = ORPOConfig(output_dir=args.out, num_train_epochs=args.epochs,
                     per_device_train_batch_size=2, gradient_accumulation_steps=8,
                     learning_rate=8e-6, beta=0.1, logging_steps=10,
                     save_steps=200, bf16=True, optim="paged_adamw_8bit")
    ORPOTrainer(model=model, args=cfg, train_dataset=ds, tokenizer=tok).train()
    model.save_pretrained(args.out)
    print(f"saved -> {args.out}")


if __name__ == "__main__":
    raise SystemExit("GPU required. Run on a GPU host, see module docstring.")
    # main()  # uncomment on a GPU host
