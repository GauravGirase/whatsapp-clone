import redis
import time
import threading
import random
import string

# Redis connection
REDIS_HOST = "redis"   # change to your service name if different
REDIS_PORT = 6379
TOTAL_REQUESTS = 10000
THREADS = 10

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

def random_string(length=20):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def worker(thread_id):
    for i in range(TOTAL_REQUESTS // THREADS):
        key = f"loadtest:{thread_id}:{i}"
        value = random_string()

        start = time.time()

        r.set(key, value)
        r.get(key)

        end = time.time()

        latency = (end - start) * 1000
        print(f"Thread {thread_id} Request {i} Latency: {latency:.2f} ms")

def main():
    threads = []

    start_time = time.time()

    for i in range(THREADS):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    end_time = time.time()

    total_time = end_time - start_time
    total_ops = TOTAL_REQUESTS * 2  # set + get

    print("\n===== Load Test Result =====")
    print(f"Total Operations: {total_ops}")
    print(f"Total Time: {total_time:.2f} seconds")
    print(f"Ops/sec: {total_ops / total_time:.2f}")

if __name__ == "__main__":
    main()
