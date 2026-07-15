from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import threading
import queue
import time
import sys
import os
import subprocess
import logging

logging.getLogger('werkzeug').setLevel(logging.ERROR)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__)
CORS(app)

task_queue = queue.Queue()
running_tasks = {}
task_id_counter = 0
max_concurrent_tasks = 4
task_status = {}
lock = threading.Lock()

def execute_booster(task_id, bv, target):
    """直接在线程中执行booster逻辑，输出保存到task_status"""
    import sys
    from io import StringIO
    
    # 检查是否已经被取消
    with lock:
        if task_id in task_status and task_status[task_id]['status'] == 'cancelled':
            return
    
    class CaptureOutput:
        def __init__(self):
            self.original_stdout = sys.stdout
        
        def write(self, s):
            with lock:
                if task_id in task_status:
                    if task_status[task_id]['status'] == 'cancelled':
                        return
                    # booster 使用 \r 来覆盖同一行更新进度
                    # 保持终端行为：遇到 \r 回到行首，只保留最新进度
                    if '\r' in s:
                        # 按 \r 分割，取最后一段，覆盖最后一行
                        lines = task_status[task_id]['output'].splitlines()
                        if lines:
                            lines[-1] = s.replace('\r', '').rstrip('\n')
                            task_status[task_id]['output'] = '\n'.join(lines) + '\n'
                        else:
                            task_status[task_id]['output'] += s.replace('\r', '\n')
                    else:
                        task_status[task_id]['output'] += s
            self.original_stdout.write(s)
        
        def flush(self):
            self.original_stdout.flush()
    
    capture = CaptureOutput()
    sys.stdout = capture
    
    try:
        with lock:
            if task_id in task_status and task_status[task_id]['status'] == 'cancelled':
                return
        
        from booster import main
        main(bv, target)
        
        with lock:
            if task_id in task_status:
                if task_status[task_id]['status'] != 'cancelled':
                    task_status[task_id]['status'] = 'completed'
                    task_status[task_id]['end_time'] = time.time()
    except Exception as e:
        with lock:
            if task_id in task_status:
                if task_status[task_id]['status'] != 'cancelled':
                    task_status[task_id]['status'] = 'error'
                    task_status[task_id]['output'] += f'\nError: {str(e)}\n'
                    task_status[task_id]['end_time'] = time.time()
    finally:
        sys.stdout = capture.original_stdout

def run_task(task):
    global task_status, running_tasks, lock
    task_id = task['id']
    bv = task['bv']
    target = task['target']
    
    with lock:
        task_status[task_id]['status'] = 'running'
        running_tasks[task_id] = threading.current_thread()
    
    try:
        execute_booster(task_id, bv, target)
    finally:
        with lock:
            if task_id in running_tasks:
                del running_tasks[task_id]
        task_queue.task_done()

def worker():
    while True:
        task = task_queue.get()
        if task is None:
            break
        threading.Thread(target=run_task, args=(task,), daemon=True).start()

for _ in range(max_concurrent_tasks):
    threading.Thread(target=worker, daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/tasks', methods=['POST'])
def create_task():
    global task_id_counter
    
    data = request.json
    bv = data.get('bv')
    target = data.get('target')
    
    if not bv or not target:
        return jsonify({'error': 'Missing BV or target'}), 400
    
    task_id_counter += 1
    task_id = task_id_counter
    
    task = {
        'id': task_id,
        'bv': bv,
        'target': target
    }
    
    with lock:
        task_queue.put(task)
        task_status[task_id] = {
            'status': 'queued',
            'output': '',
            'start_time': time.time(),
            'bv': bv,
            'target': target
        }
    
    return jsonify({'task_id': task_id, 'status': 'queued'})

@app.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task_status(task_id):
    with lock:
        if task_id not in task_status:
            return jsonify({'error': 'Task not found'}), 404
        return jsonify(task_status[task_id])

@app.route('/api/tasks', methods=['GET'])
def get_all_tasks():
    with lock:
        return jsonify(task_status)

@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
def cancel_task(task_id):
    with lock:
        if task_id in running_tasks:
            # Python线程不能强制终止，只能标记为取消，让它自然结束
            # 因为Python不支持强制终止线程，我们只能标记状态
            if task_id in task_status:
                task_status[task_id]['status'] = 'cancelled'
                task_status[task_id]['end_time'] = time.time()
            
            del running_tasks[task_id]
            return jsonify({'status': 'cancelled'})
        
    return jsonify({'error': 'Task not running'}), 400

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
