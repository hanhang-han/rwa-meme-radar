module.exports = { apps: [{
  name: 'pyradar-storage-recovery',
  cwd: '/opt/memedashboard',
  script: '/opt/memedashboard/server-py/.venv/bin/python',
  args: '-m app.storage_recovery_worker',
  interpreter: 'none',
  exec_mode: 'fork',
  instances: 1,
  autorestart: true,
  restart_delay: 5000,
  min_uptime: 30000,
  max_restarts: 10,
  env: { PYTHONPATH: '/opt/memedashboard/server-py' },
}] };
