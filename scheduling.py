from flask import Flask, request, jsonify, send_file
import psutil
import time
from threading import Thread, Lock
from collections import deque
import subprocess
import os
from pymongo import MongoClient
from flask import Response
import io
import base64
import matplotlib
matplotlib.use('Agg')  # Use Anti-Grain Geometry backend (no GUI)
import matplotlib.pyplot as plt

# Connect to MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client['admin']
metrics_collection = db['schedule']

app = Flask(__name__)

process_data = {}
timeline = deque()
monitoring = False
QUANTUM = 2
monitored_pids = []
rr_queue = deque()
timeline_data = {} 
start_time = None
monitor_lock = Lock()

def calculate_and_store_metrics(pid):
    if pid not in process_data:
        return

    pdata = process_data[pid]

    arrival_time = pdata.get("arrival_time", pdata.get("start", time.time()))
    start_time_ = pdata.get("start", 0)

    try:
        proc = psutil.Process(pid)
        cpu_times = proc.cpu_times()
        bt = round(cpu_times.user + cpu_times.system, 2)
    except psutil.NoSuchProcess:
        bt = pdata.get("bt", 0)

    # More accurate per-process completion time
    completion_time = time.time() - arrival_time
    tat = time.time() - arrival_time
    waiting_time = tat - bt
    response_time = start_time_ - arrival_time
    throughput = 1 / completion_time if completion_time else 0

    avg_cpu = sum(pdata['cpu']) / len(pdata['cpu']) if pdata['cpu'] else 0
    avg_mem = sum(pdata['mem']) / len(pdata['mem']) if pdata['mem'] else 0

    metrics = {
        "pid": pid,
        "start_time": round(start_time_, 2),
        "completion_time": round(completion_time, 2),
        "burst_time": round(bt, 2),
        "turnaround_time": round(tat, 2),
        "waiting_time": round(waiting_time, 2),
        "response_time": round(response_time, 2),
        "arrival_time": round(arrival_time, 2),
        "throughput": round(throughput, 4),
        "avg_cpu_percent": round(avg_cpu, 2),
        "avg_memory_percent": round(avg_mem, 2),
        "timestamp": time.time()
    }

    metrics_collection.insert_one(metrics)
    print(f"Metrics stored for PID {pid}")
def suggest_best_algorithm(metrics):
    if not metrics:
        return "No data available"

    def avg(lst): return sum(lst) / len(lst) if lst else float('inf')

    # Group metrics by algorithm (assumed tags or inferred)
    rr = [m for m in metrics if m.get('throughput')]  # crude Round Robin check
    sjf = sorted(metrics, key=lambda x: x['burst_time'])
    fcfs = sorted(metrics, key=lambda x: x['arrival_time'])

    rr_wt = avg([m['waiting_time'] for m in rr])
    sjf_wt = avg([m['waiting_time'] for m in sjf])
    fcfs_wt = avg([m['waiting_time'] for m in fcfs])

    rr_tat = avg([m['turnaround_time'] for m in rr])
    sjf_tat = avg([m['turnaround_time'] for m in sjf])
    fcfs_tat = avg([m['turnaround_time'] for m in fcfs])

    best = min([
        (rr_wt + rr_tat, "Round Robin"),
        (sjf_wt + sjf_tat, "SJF"),
        (fcfs_wt + fcfs_tat, "FCFS")
    ], key=lambda x: x[0])

    return best[1]
@app.route('/result')
def result():
    global process_data, monitored_pids, timeline

    # --- Step 1: Fetch Historical + Live Metrics ---
    db_metrics = list(metrics_collection.find({}, {'_id': 0}))
    live_metrics = []

    for pid in monitored_pids:
        pdata = process_data.get(pid)
        if not pdata:
            continue

        try:
            proc = psutil.Process(pid)
            cpu_times = proc.cpu_times()
            burst_time = round(cpu_times.user + cpu_times.system, 2)
            is_running = True
        except psutil.NoSuchProcess:
            burst_time = pdata.get("burst_time", 0)
            is_running = False

        now = time.time()
        arrival_time = pdata.get("arrival_time", now - 10)
        start_time_ = pdata.get("start", arrival_time + 1)
        tat = (now if is_running else pdata.get("completion_time", now)) - arrival_time
        wt = tat - burst_time
        rt = start_time_ - arrival_time

        live_metrics.append({
            'pid': pid,
            'start_time': round(start_time_, 2),
            'completion_time': round(now if not is_running else 0, 2),
            'burst_time': round(burst_time, 2),
            'turnaround_time': round(tat, 2),
            'waiting_time': round(wt, 2),
            'response_time': round(rt, 2),
            'arrival_time': round(arrival_time, 2),
            'throughput': 0,
            'avg_cpu_percent': round(sum(pdata.get('cpu', [0])) / max(1, len(pdata.get('cpu', []))), 2),
            'avg_memory_percent': round(sum(pdata.get('mem', [0])) / max(1, len(pdata.get('mem', []))), 2),
            'timestamp': now
        })

    combined_metrics = db_metrics + live_metrics
    best_algo = suggest_best_algorithm(combined_metrics)

    # --- Step 2: Simulate Scheduling Algorithms ---
    def fcfs_schedule(metrics):
        metrics.sort(key=lambda x: x['arrival_time'])
        current = 0
        schedule = []
        for proc in metrics:
            start = max(current, proc['arrival_time'])
            schedule.append({'pid': proc['pid'], 'start': start, 'duration': proc['burst_time']})
            current = start + proc['burst_time']
        return schedule

    def sjf_schedule(metrics):
        queue = sorted(metrics, key=lambda x: (x['arrival_time'], x['burst_time']))
        current = 0
        schedule = []
        pending = queue[:]
        while pending:
            available = [p for p in pending if p['arrival_time'] <= current]
            if not available:
                current = pending[0]['arrival_time']
                continue
            shortest = min(available, key=lambda x: x['burst_time'])
            start = max(current, shortest['arrival_time'])
            schedule.append({'pid': shortest['pid'], 'start': start, 'duration': shortest['burst_time']})
            current = start + shortest['burst_time']
            pending.remove(shortest)
        return schedule

    def rr_schedule(metrics, quantum=2):
        queue = sorted(metrics, key=lambda x: x['arrival_time'])
        time_line = []
        time = 0
        ready = []
        waiting = queue[:]
        remaining = {p['pid']: p['burst_time'] for p in queue}
        arrival_map = {p['pid']: p['arrival_time'] for p in queue}

        while waiting or ready:
            while waiting and waiting[0]['arrival_time'] <= time:
                ready.append(waiting.pop(0))
            if not ready:
                time = waiting[0]['arrival_time']
                continue
            proc = ready.pop(0)
            pid = proc['pid']
            start = time
            run_time = min(quantum, remaining[pid])
            time += run_time
            remaining[pid] -= run_time
            time_line.append({'pid': pid, 'start': start, 'duration': run_time})
            if remaining[pid] > 0:
                while waiting and waiting[0]['arrival_time'] <= time:
                    ready.append(waiting.pop(0))
                ready.append(proc)
        return time_line

    # --- Step 3: Gantt Chart Generator ---
    def generate_gantt_chart(schedule, title):
        fig, ax = plt.subplots(figsize=(12, 5))
        colors = {}

        unique_pids = list({task['pid'] for task in schedule})
        pid_y = {pid: idx * 10 for idx, pid in enumerate(unique_pids)}

        for task in schedule:
            pid = task['pid']
            start = task['start']
            duration = task['duration']
            if pid not in colors:
                colors[pid] = f"C{len(colors)}"
            ax.broken_barh([(start, duration)], (pid_y[pid], 9), facecolors=colors[pid])
            ax.text(start + duration / 2, pid_y[pid] + 4.5, f"{pid}", ha='center', va='center', fontsize=9, color='black')

        ax.set_title(title)
        ax.set_xlabel('Time')
        ax.set_ylabel('Processes')
        ax.set_yticks([pid_y[pid] + 4.5 for pid in unique_pids])
        ax.set_yticklabels([f"PID {pid}" for pid in unique_pids])
        ax.grid(True)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png')
        buf.seek(0)
        img_data = base64.b64encode(buf.read()).decode('utf-8')
        plt.close(fig)
        return img_data

    # --- Step 4: Run All Scheduling and Visualize ---
    fcfs = fcfs_schedule(combined_metrics)
    sjf = sjf_schedule(combined_metrics)
    rr = rr_schedule(combined_metrics)

    fcfs_img = generate_gantt_chart(fcfs, "FCFS Scheduling")
    sjf_img = generate_gantt_chart(sjf, "SJF Scheduling")
    rr_img = generate_gantt_chart(rr, "Round Robin Scheduling")

    return jsonify({
        'best_algo': best_algo,
        'fcfs_img': fcfs_img,
        'sjf_img': sjf_img,
        'rr_img': rr_img,
        'metrics': combined_metrics
    })
def monitor_processes(pids):
    global process_data, timeline, monitoring, start_time, rr_queue

    monitoring = True
    start_time = time.time()

    while monitoring:
        if not rr_queue:
            time.sleep(0.1)
            continue

        pid = rr_queue.popleft()

        if not psutil.pid_exists(pid):
            continue

        try:
            p = psutil.Process(pid)

            if process_data[pid]['start'] is None:
                process_data[pid]['start'] = time.time() - start_time

            cpu_usage = []
            mem_usage = []

            end_time = time.time() + QUANTUM
            while time.time() < end_time:
                cpu = p.cpu_percent(interval=0.1)
                mem = p.memory_percent()
                cpu_usage.append(cpu)
                mem_usage.append(mem)

            bt = p.cpu_times().user + p.cpu_times().system
            process_data[pid]['bt'] = bt
            process_data[pid]['cpu'].append(sum(cpu_usage) / len(cpu_usage) if cpu_usage else 0)
            process_data[pid]['mem'].append(sum(mem_usage) / len(mem_usage) if mem_usage else 0)

            elapsed_time = time.time() - start_time
            timeline.append({
                'pid': pid,
                'timestamp': round(elapsed_time, 2),
                'duration': QUANTUM,
                'status': 'Running'
            })

            if p.is_running():
                rr_queue.append(pid)

            time.sleep(0.05)

        except psutil.NoSuchProcess:
            elapsed_time = time.time() - start_time
            timeline.append({
                'pid': pid,
                'timestamp': round(elapsed_time, 2),
                'duration': QUANTUM,
                'status': 'Terminated'
            })


@app.route('/')
def serve_ui():
    return send_file('pj2.html')


@app.route('/start', methods=['POST'])
def start():
    global monitoring, monitored_pids, rr_queue

    data = request.get_json()
    new_pids = data.get('pids', [])

    if not new_pids:
        return jsonify({'error': 'No PIDs provided'}), 400

    with monitor_lock:
        for pid in new_pids:
            if pid not in monitored_pids:
                monitored_pids.append(pid)
                rr_queue.append(pid)
                process_data[pid] = {'cpu': [], 'mem': [], 'start': None, 'bt': 0}

    # Restart monitoring
    monitoring = False
    time.sleep(1)

    thread = Thread(target=monitor_processes, args=(monitored_pids,))
    thread.start()
    return jsonify({'message': 'Monitoring updated with new PIDs'})


@app.route('/generate-dummy', methods=['POST'])
def generate_dummy():
    global monitored_pids, rr_queue

    count = request.json.get('count', 0)
    generated_pids = []

    for _ in range(count):
        proc = subprocess.Popen(['python3', '-c', 'import time; time.sleep(300)'])
        generated_pids.append(proc.pid)

    with monitor_lock:
        for pid in generated_pids:
            if pid not in monitored_pids:
                monitored_pids.append(pid)
                rr_queue.append(pid)
                process_data[pid] = {'cpu': [], 'mem': [], 'start': None, 'bt': 0}

    return jsonify({
        'message': f'{count} dummy processes started.',
        'pids': generated_pids
    })


@app.route('/live')
def live():
    global monitored_pids
    result = {}

    if not monitored_pids:
        return jsonify({'error': 'Monitoring has not started yet'}), 200

    for pid in monitored_pids:
        try:
            p = psutil.Process(pid)
            result[pid] = {
                'cpu': p.cpu_percent(interval=0.1),
                'mem': p.memory_percent(),
                'name': p.name(),
                'status': p.status(),
                'priority': p.nice(),
                'threads': p.num_threads(),
                'cpu_times': p.cpu_times()._asdict(),
                'net_io': psutil.net_io_counters()._asdict(),
                'env': p.environ()
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            result[pid] = {'error': 'Process not accessible or not found'}

    return jsonify(result)


@app.route('/timeline')
def get_timeline():
    return jsonify(list(timeline))
@app.route('/reset', methods=['POST'])
def reset():
    global monitored_pids, rr_queue, process_data, timeline, monitoring

    with monitor_lock:
        # 💥 Terminate all monitored processes first
        for pid in list(monitored_pids):
            try:
                psutil.Process(pid).terminate()
            except Exception as e:
                print(f"Failed to terminate PID {pid}: {e}")

        # 🧹 Clear all runtime tracking
        monitored_pids.clear()
        rr_queue.clear()
        process_data.clear()
        timeline.clear()
        monitoring = False  # Stops the monitoring loop

        # 🗑️ Clear MongoDB data
        metrics_collection.delete_many({})  # Delete all records from 'schedule'

    return jsonify({'message': 'All processes terminated and monitoring reset successfully 💫'})
@app.route('/terminate', methods=['POST'])
def terminate():
    data = request.get_json()
    pids = data.get('pids', [])  # Accepts a list now

    if not pids:
        return jsonify({'message': 'No PIDs provided'}), 400

    terminated = []
    failed = []

    with monitor_lock:
        for pid in pids:
            try:
                pid = int(pid)

                if not psutil.pid_exists(pid):
                    failed.append({'pid': pid, 'error': 'No such process'})
                    continue

                proc = psutil.Process(pid)
                proc.terminate()
                
                try:
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)

                # Clean from memory
                if pid in monitored_pids:
                    monitored_pids.remove(pid)
                if pid in rr_queue:
                    try:
                        rr_queue.remove(pid)
                    except ValueError:
                        pass  # PID might not be in queue anymore

                # Store metrics even if process already exited
                calculate_and_store_metrics(pid)
                terminated.append(pid)

            except psutil.AccessDenied:
                failed.append({'pid': pid, 'error': 'Access denied'})
            except psutil.NoSuchProcess:
                calculate_and_store_metrics(pid)  # Might have already exited, still store data
                terminated.append(pid)
            except Exception as e:
                failed.append({'pid': pid, 'error': str(e)})

    return jsonify({
        'terminated': terminated,
        'failed': failed,
        'message': f"Terminated {len(terminated)} processes. {len(failed)} failures."
    })
if __name__ == '__main__':
    app.run(debug=True, port=5007)