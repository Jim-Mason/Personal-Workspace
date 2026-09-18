/**
 * OpsGen - Live script preview and parameter presets
 */
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('template-form');
  const previewCode = document.getElementById('live-preview-code');
  const previewStatus = document.getElementById('preview-status');
  const savePresetBtn = document.getElementById('save-preset');
  const loadPresetBtn = document.getElementById('load-preset');

  if (!form || !previewCode) return;

  const templateName = form.dataset.template;
  const presetKey = `opsgen_preset_${templateName}`;
  let debounceTimer = null;

  function collectParams() {
    const params = {};
    form.querySelectorAll('[name]').forEach((el) => {
      if (el.type === 'checkbox' && el.closest('.checkbox-group')) {
        if (!params[el.name]) params[el.name] = [];
        if (el.checked) params[el.name].push(el.value);
      } else if (el.type === 'checkbox') {
        params[el.name] = el.checked;
      } else if (el.type !== 'checkbox') {
        params[el.name] = el.value;
      }
    });
    return params;
  }

  async function refreshPreview() {
    previewStatus.textContent = '生成中...';
    try {
      const response = await fetch('/api/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template: templateName, params: collectParams() }),
      });
      const data = await response.json();
      if (response.ok) {
        const script = data.outputs?.script || '';
        previewCode.textContent = script;
        previewCode.className = 'language-bash';
        if (window.hljs) hljs.highlightElement(previewCode);
        previewStatus.textContent = '已更新';
      } else {
        previewStatus.textContent = data.error || '预览失败';
      }
    } catch {
      previewStatus.textContent = '网络错误';
    }
  }

  function schedulePreview() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refreshPreview, 500);
  }

  form.querySelectorAll('input, select').forEach((el) => {
    el.addEventListener('input', schedulePreview);
    el.addEventListener('change', schedulePreview);
  });

  savePresetBtn?.addEventListener('click', () => {
    localStorage.setItem(presetKey, JSON.stringify(collectParams()));
    OpsGen._toast('参数已保存到浏览器');
  });

  loadPresetBtn?.addEventListener('click', () => {
    const raw = localStorage.getItem(presetKey);
    if (!raw) {
      OpsGen._toast('暂无保存的参数');
      return;
    }
    const params = JSON.parse(raw);
    Object.entries(params).forEach(([name, value]) => {
      const fields = form.querySelectorAll(`[name="${name}"]`);
      if (!fields.length) return;
      if (fields[0].type === 'checkbox' && fields[0].closest('.checkbox-group')) {
        fields.forEach((field) => {
          field.checked = Array.isArray(value) && value.includes(field.value);
        });
      } else if (fields[0].type === 'checkbox') {
        fields[0].checked = Boolean(value);
      } else {
        fields[0].value = value;
      }
    });
    form.dispatchEvent(new Event('input', { bubbles: true }));
    schedulePreview();
    OpsGen._toast('参数已加载');
  });

  schedulePreview();
});
