"""
make_colab_notebook.py -- Build a self-contained Colab notebook for the GPU step.

The notebook (colab_finetune.ipynb):
  1. checks for a GPU (Colab free T4 expected)
  2. generates the synthetic ORPO preference dataset on the spot (no files needed)
  3. installs training deps
  4. QLoRA + ORPO fine-tunes Qwen2.5-7B-Instruct for citation discipline
  5. runs a quick behavioral eval (citation present? refusal exact?)

Why 7B and not 14B: a free Colab T4 has ~15GB VRAM. 14B QLoRA+ORPO does not
fit reliably; 7B with batch-size 1 + gradient accumulation does. This is a
deliberate, documented trade-off vs the DeepSeek plan.

Run: python3 scripts/make_colab_notebook.py  ->  colab_finetune.ipynb
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from make_finetune_data import SUMMARIES, QUESTION_TEMPLATES, RAMBLE_PREFIX, SYSTEM


def md(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(keepends=True)}


def code(source: str) -> dict:
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": source.splitlines(keepends=True)}


CELL_0 = """# Tradeify Risk Copilot — QLoRA + ORPO fine-tune (Google Colab, free T4)

**What this does:** teaches Qwen2.5-7B-Instruct *citation discipline* — every factual
claim carries a `[chunk_xxx]` citation, and out-of-rulebook questions get the exact
refusal string. Knowledge stays in the RAG corpus; the weights learn *behavior*.

**You need:** a Colab runtime with GPU (Runtime → Change runtime type → T4 GPU).
Expected wall time: 2–4 hours. Colab free may disconnect idle sessions — keep the
tab open. Nothing here costs money.

**Trade-off (documented):** the DeepSeek plan called for 14B. A free T4 (~15 GB VRAM)
cannot reliably fit 14B QLoRA+ORPO, so this notebook uses **7B** with batch-size 1 and
gradient accumulation. Same method, smaller model, actually runnable for free.
"""

CELL_1 = """import torch
assert torch.cuda.is_available(), "No GPU — set Runtime → Change runtime type → GPU (T4)."
name = torch.cuda.get_device_name(0)
gb = torch.cuda.get_device_properties(0).total_memory / 1e9
print(f"GPU: {name} ({gb:.1f} GB)")
"""

CELL_2 = (
    "# ---- synthetic ORPO preference dataset (generated on the spot) ----\n"
    "SUMMARIES = " + repr(SUMMARIES) + "\n"
    "QUESTION_TEMPLATES = " + repr(QUESTION_TEMPLATES) + "\n"
    "RAMBLE_PREFIX = " + repr(RAMBLE_PREFIX) + "\n"
    "import json, random\n"
    "rng = random.Random(7)\n"
    "ids = list(SUMMARIES)\n"
    "pairs = []\n"
    "for cid in ids:\n"
    "    title, short, summary = SUMMARIES[cid]\n"
    "    context = f'[{cid}] {title}: {summary}'\n"
    "    for tmpl in QUESTION_TEMPLATES:\n"
    "        q = tmpl.format(title=title, short=short)\n"
    "        chosen = f'{summary} [{cid}]'\n"
    "        wrong = rng.choice([i for i in ids if i != cid])\n"
    "        for rej in (summary, f'{summary} [{wrong}]', RAMBLE_PREFIX + summary):\n"
    "            pairs.append({'context': context, 'question': q,\n"
    "                          'chosen': chosen, 'rejected': rej})\n"
    "rng.shuffle(pairs)\n"
    "SYSTEM = " + repr(SYSTEM) + "\n"
    "PROMPT = '<system>{system}</system>\\n<context>{context}</context>\\n<question>{question}</question>'\n"
    "rows = [{'prompt': PROMPT.format(system=SYSTEM, context=p['context'], question=p['question']),\n"
    "         'chosen': p['chosen'], 'rejected': p['rejected']} for p in pairs]\n"
    "with open('/content/finetune_pairs.jsonl', 'w') as f:\n"
    "    f.write('\\n'.join(json.dumps(r) for r in rows))\n"
    "print(f'wrote {len(rows)} preference pairs')\n"
    "print('chosen :', rows[0]['chosen'][:110], '...')\n"
    "print('rejected:', rows[0]['rejected'][:110], '...')\n"
)

CELL_3 = """!pip install -q torch transformers peft trl datasets bitsandbytes accelerate
print("deps installed")"""

CELL_4 = """# ---- QLoRA + ORPO training (the long cell; go make coffee) ----
import torch
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig, prepare_model_for_kbit_training, get_peft_model
from trl import ORPOConfig, ORPOTrainer

MODEL = "Qwen/Qwen2.5-7B-Instruct"
OUT = "/content/qwen7b-copilot-orpo"

bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                         bnb_4bit_compute_dtype=torch.bfloat16)
model = AutoModelForCausalLM.from_pretrained(MODEL, quantization_config=bnb, device_map="auto")
tok = AutoTokenizer.from_pretrained(MODEL)
tok.pad_token = tok.eos_token or tok.pad_token
model = prepare_model_for_kbit_training(model)
model = get_peft_model(model, LoraConfig(
    r=32, lora_alpha=64, lora_dropout=0.05, task_type="CAUSAL_LM",
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]))

ds = load_dataset("json", data_files="/content/finetune_pairs.jsonl")["train"]
cfg = ORPOConfig(output_dir=OUT, num_train_epochs=2,
                 per_device_train_batch_size=1, gradient_accumulation_steps=16,
                 learning_rate=8e-6, beta=0.1, max_length=1024,
                 logging_steps=10, save_steps=200, bf16=True,
                 optim="paged_adamw_8bit", remove_unused_columns=False)
ORPOTrainer(model=model, args=cfg, train_dataset=ds, tokenizer=tok).train()
model.save_pretrained(OUT)
tok.save_pretrained(OUT)
print(f"saved -> {OUT}  (download this folder before the session ends!)")
"""

CELL_5 = """# ---- quick behavioral eval: citation present? refusal exact? ----
import re, torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

BASE = "Qwen/Qwen2.5-7B-Instruct"
ADAPT = "/content/qwen7b-copilot-orpo"
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                         bnb_4bit_compute_dtype=torch.bfloat16)
base = AutoModelForCausalLM.from_pretrained(BASE, quantization_config=bnb, device_map="auto")
tok = AutoTokenizer.from_pretrained(BASE)
model = PeftModel.from_pretrained(base, ADAPT)

CTX = "[chunk_dll] Daily Loss Limit: Growth accounts have a daily loss limit ($1,250 on $50K) that pauses trading for the session when hit but never fails the account."
SYS = "You are the Tradeify Risk Copilot. Answer ONLY from the context. Cite every factual claim like [chunk_dll]."
REFUSAL = "I don't have that in the Tradeify rulebook I can access."

def ask(question, context=CTX):
    prompt = f"<system>{SYS}</system>\\n<context>{context}</context>\\n<question>{question}</question>\\n<answer>"
    ids = tok(prompt, return_tensors="pt").to(model.device)
    out = model.generate(**ids, max_new_tokens=120, do_sample=False)
    return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True).strip()

tests = [
    ("What is the daily loss limit?", lambda a: bool(re.search(r"\\[chunk_dll\\]", a)), "citation present"),
    ("Explain the DLL.", lambda a: bool(re.search(r"\\[chunk_[a-z_]+\\]", a)), "any citation present"),
    ("What is the capital gains tax in Indonesia?", lambda a: a.strip() == REFUSAL, "exact refusal"),
    ("Who won the 2024 World Series?", lambda a: a.strip() == REFUSAL, "exact refusal (OOD)"),
]
ok = 0
for q, check, name in tests:
    a = ask(q, context="" if "tax" in q or "World Series" in q else CTX)
    passed = check(a)
    ok += passed
    print(f"[{'PASS' if passed else 'FAIL'}] {name}\\n  Q: {q}\\n  A: {a[:160]}\\n")
print(f"{ok}/{len(tests)} behavioral checks passed")
"""

CELL_6 = """## Next steps

1. **Download `/content/qwen7b-copilot-orpo`** (the LoRA adapter) before the Colab
   session ends — files vanish when the runtime is recycled.
2. Merge or load it with PEFT, then serve it (Ollama `Modelfile` / vLLM / llama.cpp)
   and point the agent's `explain` tier at it (`src/model_router.py`).
3. Re-run the eval harness against the tuned model and compare `citation_precision`
   and `refusal_accuracy` vs the base model — that delta is your interview story.
4. Better data → better model: replace the synthetic pairs with frontier-model
   generated ones (500+ pairs) and re-run this notebook unchanged.
"""


def main() -> None:
    nb = {"nbformat": 4, "nbformat_minor": 5,
          "metadata": {"kernelspec": {"display_name": "Python 3 (Colab)",
                                      "language": "python", "name": "python3"},
                       "accelerator": "GPU"},
          "cells": [md(CELL_0), code(CELL_1), code(CELL_2),
                    code(CELL_3), code(CELL_4), code(CELL_5), md(CELL_6)]}
    out = os.path.join(os.path.dirname(__file__), "..", "colab_finetune.ipynb")
    with open(out, "w") as f:
        json.dump(nb, f, indent=1)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
