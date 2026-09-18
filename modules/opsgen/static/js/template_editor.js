/**
 * OpsGen - Custom template YAML editor
 */
document.addEventListener('DOMContentLoaded', () => {
  const validateBtn = document.getElementById('validate-btn');
  const yamlContent = document.getElementById('yaml-content');
  const resultBox = document.getElementById('validate-result');

  if (!validateBtn || !yamlContent) return;

  validateBtn.addEventListener('click', async () => {
    resultBox.classList.remove('hidden', 'alert-error', 'alert-success');
    resultBox.textContent = '验证中...';

    try {
      const response = await fetch('/api/templates/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml_content: yamlContent.value }),
      });
      const data = await response.json();
      if (response.ok) {
        resultBox.classList.add('alert-success');
        resultBox.textContent = `✓ 验证通过：${data.title || data.name}（${data.question_count} 个问题）`;
      } else {
        resultBox.classList.add('alert-error');
        resultBox.textContent = `✗ ${data.error || '验证失败'}`;
      }
    } catch {
      resultBox.classList.add('alert-error');
      resultBox.textContent = '✗ 网络错误，请稍后重试';
    }
  });

  yamlContent.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const start = yamlContent.selectionStart;
      const end = yamlContent.selectionEnd;
      yamlContent.value = `${yamlContent.value.substring(0, start)}  ${yamlContent.value.substring(end)}`;
      yamlContent.selectionStart = yamlContent.selectionEnd = start + 2;
    }
  });

  const importInput = document.getElementById('import-yaml');
  importInput?.addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      yamlContent.value = reader.result;
      OpsGen._toast(`已导入 ${file.name}`);
    };
    reader.readAsText(file);
  });
});
