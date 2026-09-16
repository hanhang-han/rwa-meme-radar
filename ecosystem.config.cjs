// Explicit runtime: PM2's inherited PATH previously selected Node 20.
module.exports = { apps: [{
  name: 'memedashboard',
  cwd: '/opt/memedashboard',
  script: 'src/server.ts',
  interpreter: '/www/server/nodejs/v22.22.0/bin/node',
  node_args: '--import tsx',
  exec_mode: 'fork', instances: 1, autorestart: true,
}] };
