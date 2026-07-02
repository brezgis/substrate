import csv

unique_combinations = set()
with open('data.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        # Use strings for task_id since CSV reads them as such
        unique_combinations.add((row['task_id'], row['style']))

print(f"Total unique (task_id, style) combinations found: {len(unique_combinations)}")
print("Found:", sorted(list(unique_combinations)))

expected = [
    ('1', 'Direct'), ('2', 'Direct'), ('3', 'Direct'), ('4', 'Direct'), ('5', 'Direct'),
    ('1', 'Instructional'), ('2', 'Instructional'), ('3', 'Instructional'), ('4', 'Instructional'), ('5', 'Instructional'),
    ('1', 'Verbose'), ('2', 'Verbose'), ('3', 'Verbose'), ('4', 'Verbose'), ('5', 'Verbose'),
    ('1', 'Few-Shot'), ('2', 'Few-Shot'), ('3', 'Few-Shot'), ('4', 'Few-Shot'), ('5', 'Few-Shot')
]

missing = [e for e in expected if e not in unique_combinations]
if missing:
    print(f"Missing combinations: {missing}")
else:
    print("All 20 combinations collected.")
