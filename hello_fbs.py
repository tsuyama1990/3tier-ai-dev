import random

class FBSModel:
    def __init__(self):
        self.states = ['R', 'A', 'B']
        self.transition_matrix = {
            'R': {'R': 0.5, 'A': 0.3, 'B': 0.2},
            'A': {'R': 0.4, 'A': 0.4, 'B': 0.2},
            'B': {'R': 0.1, 'A': 0.6, 'B': 0.3}
        }
        self.state_distribution = {state: 1/len(self.states) for state in self.states}

    def simulate_transitions(self, num_transitions):
        current_state = random.choices(list(self.state_distribution.keys()), list(self.state_distribution.values()))[0]
        print(f"Starting from state: {current_state}")
        
        for _ in range(num_transitions):
            transition_probabilities = self.transition_matrix[current_state]
            next_state = random.choices(list(transition_probabilities.keys()), list(transition_probabilities.values()))[0]
            print(f"Transitioned to state: {next_state}")
            current_state = next_state
        
        return self.state_distribution

if __name__ == '__main__':
    fbs_model = FBSModel()
    final_distribution = fbs_model.simulate_transitions(20)
    print("Final state distribution:")
    for state, count in final_distribution.items():
        print(f"{state}: {count}")
