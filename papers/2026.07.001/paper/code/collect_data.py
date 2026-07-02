import requests
import json
import numpy as np

def call_api(prompt):
    url = "http://127.0.0.1:11434/api/generate"
    payload = {
        "model": "gemma4:12b",
        "prompt": prompt,
        "stream": False
    }
    try:
        response = requests.post(url, json=payload)
        return response.json()['response'].strip()
    except Exception as e:
        print(f"Error: {e}")
        return ""

text_to_summarize = "Quantum computing is a type of computation that uses quantum-mechanical phenomena, such as superposition and entanglement, to perform calculations. While classical computers use bits (0 or 1), quantum computers use qubits."

prompts = {
    "neutral": "Summarize the following text: " + text_to_summarize,
    "persona": "You are an expert technical writer. Summarize the following text: " + text_to_summarize,
    "constrained": "Summarize the following text in exactly three sentences using professional language: " + text_to_summarize
}

results = {}
for key, p in prompts.items():
    outputs = []
    for i in range(3):
        out = call_api(p)
        outputs.append(out)
    results[key] = outputs

stats = {}
for key, outputs in results.items():
    lengths = [len(o) for o in outputs]
    stats[key] = {
        "responses": outputs,
        "length_variance": np.var(lengths),
        "mean_length": np.mean(lengths)
    }

with open('data.json', 'w') as f:
    json.dump(stats, f)

print("Data collection complete.")
for k in results.keys():
    print(f"{k}: Variance={stats[k]['length_variance']:.2f}, Mean={stats[k]['mean_length']:.2f}")
