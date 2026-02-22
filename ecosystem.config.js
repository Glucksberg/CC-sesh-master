// PM2 configuration for running claude-tracker in daemon mode
// Usage: pm2 start ecosystem.config.js

const path = require('path');
const scriptDir = __dirname;

module.exports = {
  apps: [{
    name: 'claude-tracker',
    script: path.join(scriptDir, 'claude-tracker.sh'),
    args: '--daemon',
    interpreter: '/bin/bash',
    cwd: scriptDir,
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    watch: false,
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    error_file: path.join(scriptDir, 'logs', 'pm2-error.log'),
    out_file: path.join(scriptDir, 'logs', 'pm2-out.log'),
    merge_logs: true,
    env: {
      NODE_ENV: 'production'
    }
  }]
};
