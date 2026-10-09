import numpy as np

def detect_anomaly(previous_values, new_value):

    if len(previous_values) < 5:
        return False

    mean = np.mean(previous_values)
    std = np.std(previous_values)

    threshold = mean + (3 * std)

    if new_value > threshold:
        return True

    return False