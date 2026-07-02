import requests
import json
import csv
import sys
import os

def call_api(prompt):
    url = "http://127.0.0.1:11434/api/generate"
    payload = {
        "model": "gemma4:12b",
        "prompt": prompt,
        "stream": False,
        "temperature": 0.8
    }
    try:
        response = requests.post(url, json=payload)
        return response.json().get("response", "")
    except Exception as e:
        return f"Error: {e}"

tasks = [
    {"id": 1, "q": "All humans are mortal. Socrates is a human. Is Socrates mortal?"},
    {"id": 2, "q": "If some birds can fly and all eagles are birds, can all eagles fly?"},
    {"id": 3, "q": "A farmer has 10 chickens and 5 cows. How many legs do the animals have in total?"},
    {"id": 4, "q": "The next number in the sequence 2, 4, 8, 16 is what?"},
    {"id": 5, "q": "If I have three apples and you give me two more, how many do I have?"}
]

styles = {
    "Direct": "",
    "Instructional": "Let's think step by step to solve this: ",
    "Verbose": "I need your help with a logic puzzle. Please analyze the following carefully and provide the final answer clearly: ",
    "Few-Shot": "Example 1: Q: All men are mortal. Paul is a man. Is Paul mortal? A: Yes.\nExample 2: Q: If a dog is brown, and Max is that dog, what color is Max? A: Brown.\nQuestion: "
}

if len(sys.argv) < 2:
    print("Usage: python3 data_collection.py [Direct|Instructional|Verbose|Few-Shot] [TaskIndex 0-4]")
    sys.exit(1)

style_name = sys.argv[1]
task_idx = int(sys.argv[2]) if len(sys.argv) > 2 else -1

prefix = styles.get(style_name, "")
target_task = tasks[task_idx] if task_idx != -1 else None

results = []
print(f"Starting collection for style: {style_name}")
if target_task is not None:
    print(f"Targeting Task ID: {target_task['id']}")

# If no specific task is chosen, loop through all (this may time out if too many)
tasks_to_run = [target_task] if target_task is not None else tasks

for task in tasks_to_run:
    print(f"  Processing Task {task['id']}...")
    for i in range(20):
        prompt = prefix + task["q"]
        response = call_api(prompt)
        results.append({
            "task_id": task["id"],
            "style": style_name,
            "response": response
        })
        if (i+1) % 5 == 0:
            print(f"    Progress: {i+1}/20")

# Append to existing file if it exists, or create new one
file_exists = os.path.isfile('data.csv')
with open('data.csv', 'a', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=["task_id", "style", "response"])
    if not file_exists or os.path.getsize('data.csv') == 0:
        writer.writeheader()
    writer.writerows(results)

print(f"Completed collection for style: {style_name}")
