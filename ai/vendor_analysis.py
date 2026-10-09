import numpy as np

def analyze_vendor(values):

    if not values:
        return None

    avg = np.mean(values)
    std = np.std(values)

    if avg == 0:
        consistency = 0
    else:
        consistency = max(0, 100 - ((std / avg) * 100))

    if consistency >= 90:
        rating = "Excellent"
    elif consistency >= 70:
        rating = "Good"
    else:
        rating = "Unstable"

    return {
        "average": round(avg, 2),
        "consistency": round(consistency, 2),
        "rating": rating
    }