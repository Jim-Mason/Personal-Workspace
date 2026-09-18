/**
 * OpsGen - WebSocket script execution
 */
OpsGen.socket = null;

OpsGen.executeScript = function (elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;

  const script = el.textContent;
  const panel = document.getElementById('execution-panel');
  const output = document.getElementById('execution-output');
  const btn = document.getElementById('execute-btn');

  if (!confirm('⚠️ 在线执行将在服务器上运行此脚本，请确认脚本内容安全。是否继续？')) {
    return;
  }

  panel.classList.remove('hidden');
  output.textContent = '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ 执行中...';
  }

  if (!this.socket) {
    this.socket = io();
    this.socket.on('execution_output', (msg) => {
      const prefix = msg.type === 'error' ? '[ERROR] ' : msg.type === 'info' ? '[INFO] ' : '';
      output.textContent += prefix + msg.data;
      output.scrollTop = output.scrollHeight;
    });
    this.socket.on('execution_done', (data) => {
      output.textContent += `\n[EXIT] code: ${data.code}\n`;
      if (btn) {
        btn.disabled = false;
        btn.textContent = '▶️ 在线执行';
      }
    });
  }

  this.socket.emit('execute_script', { script });
};

OpsGen.closeExecution = function () {
  document.getElementById('execution-panel')?.classList.add('hidden');
};
