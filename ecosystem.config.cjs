// Explicit runtime: PM2's inherited PATH previously selected Node 20.
module.exports = { apps: [
  {
    name: 'memedashboard',
    cwd: '/opt/memedashboard',
    script: 'src/server.ts',
    interpreter: '/www/server/nodejs/v22.22.0/bin/node',
    node_args: '--import tsx',
    exec_mode: 'fork', instances: 1, autorestart: true,
  },
  {
    name: 'pyradar',
    cwd: '/opt/memedashboard',
    script: '/opt/memedashboard/server-py/.venv/bin/uvicorn',
    args: 'app.main:app --app-dir server-py --host 127.0.0.1 --port 8010',
    interpreter: 'none', exec_mode: 'fork', instances: 1, autorestart: true,
  },
  {
    name: 'pyradar-worker',
    cwd: '/opt/memedashboard',
    script: '/opt/memedashboard/server-py/.venv/bin/python',
    args: '-m app.worker',
    interpreter: 'none', exec_mode: 'fork', instances: 1, autorestart: true,
    env: { PYTHONPATH: '/opt/memedashboard/server-py', WORKER_HEALTH_PATH: 'data/worker-health.json' },
  },
  {
    name: 'pyradar-projection',
    cwd: '/opt/memedashboard',
    script: '/opt/memedashboard/server-py/.venv/bin/python',
    args: '-m app.projection_worker',
    interpreter: 'none', exec_mode: 'fork', instances: 1, autorestart: true,
    env: { PYTHONPATH: '/opt/memedashboard/server-py', WORKER_HEALTH_PATH: 'data/projection-health.json' },
  },
] };
