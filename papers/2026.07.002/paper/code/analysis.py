import csv
import matplotlib.pyplot as plt
import numpy as np

def evaluate_results():
    results = []
    with open('data.csv', 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            task_id = int(row['task_id'])
            style = row['style']
            resp = row['response'].lower()
            
            correct = False
            if task_id == 1: # Socrates
                if "yes" in resp or "true" in resp:
                    correct = True
            elif task_id == 2: # Logic Gap (No/Not necessarily)
                # Improved rule: check for specific logical rejection phrases
                if any(phrase in resp for phrase in ["not necessarily", "cannot conclude", "no, it is not clear"]):
                    correct = True
                elif "no" in resp and "maybe" not in resp: # Fallback for simple no
                     # Check if the model just says 'No' or 'Yes' without ambiguity. 
                     # In our case, we want to see if it correctly identifies the lack of certainty.
                     if "not necessarily" in resp or "cannot" in resp:
                        correct = True
            elif task_id == 3: # Legs (40)
                if "40" in resp:
                    correct = True
            elif task_id == 4: # Sequence (32)
                if "32" in resp:
                    correct = True
            elif task_id == 5: # Arithmetic (5)
                # Improved rule: accept both digits and words
                if "5" in resp or "five" in resp:
                    correct = True
            
            results.append({
                "task_id": task_id,
                "style": style,
                "correct": correct
            })

    # Group results by (task_id, style)
    stats = {}
    for r in results:
        key = (r['task_id'], r['style'])
        if key not in stats:
            stats[key] = {"total": 0, "correct": 0}
        stats[key]["total"] += 1
        if r['correct']:
            stats[key]["correct"] += 1

    print(f"{'Task':<10} | {'Style':<15} | {'Correct':<10} | {'Accuracy':<10}")
    print("-" * 50)
    sorted_keys = sorted(stats.keys())
    for (tid, style) in sorted_keys:
        data = stats[(tid, style)]
        accuracy = (data['correct'] / data['total']) * 100
        print(f"{tid:<10} | {style:<15} | {data['correct']:>8}/{data['total']} | {accuracy:>9.2f}%")

    # Plotting
    styles = ["Direct", "Instructional", "Verbose", "Few-Shot"]
    tasks = [1, 2, 3, 4, 5]
    
    data_matrix = []
    for t in tasks:
        row = []
        for s in styles:
            key = (t, s)
            if key in stats:
                row.append(stats[key]['correct'] / stats[key]['total'])
            else:
                row.append(0.0)
        data_matrix.append(row)

    plt.figure(figsize=(10, 6))
    x = np.arange(len(tasks))
    width = 0.2
    
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    for i, s_name in enumerate(styles):
        plt.bar(x + i*width, [data_matrix[j][i] for j in range(len(tasks))], 
                 width, label=s_name, color=colors[i])

    plt.xlabel('Task ID')
    plt.ylabel('Accuracy')
    plt.title('Model Accuracy across Different Prompt Styles')
    plt.xticks(x, tasks)
    plt.legend()
    plt.ylim(0, 1.1)
    plt.tight_layout()
    plt.savefig('paper/figures/accuracy_by_style.png')

if __name__ == "__main__":
    evaluate_results()
