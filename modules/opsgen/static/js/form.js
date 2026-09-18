/**
 * OpsGen - Dynamic form validation and conditional fields
 */
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('template-form');
  if (!form) return;

  const groups = form.querySelectorAll('.form-group[data-show-when]');

  function getFieldValue(name) {
    const el = form.querySelector(`[name="${name}"]`);
    if (!el) return null;

    if (el.type === 'checkbox' && !el.closest('.checkbox-group')) {
      return el.checked;
    }

    if (el.type === 'checkbox' && el.closest('.checkbox-group')) {
      return Array.from(form.querySelectorAll(`[name="${name}"]:checked`)).map((c) => c.value);
    }

    if (el.type === 'number') {
      return el.value === '' ? null : Number(el.value);
    }

    return el.value;
  }

  function evaluateShowWhen(showWhen) {
    if (!showWhen || Object.keys(showWhen).length === 0) return true;

    return Object.entries(showWhen).every(([field, expected]) => {
      const actual = getFieldValue(field);
      if (Array.isArray(expected)) {
        return expected.includes(actual);
      }
      return actual === expected || String(actual) === String(expected);
    });
  }

  function updateVisibility() {
    groups.forEach((group) => {
      let showWhen = {};
      try {
        showWhen = JSON.parse(group.dataset.showWhen || '{}');
      } catch {
        showWhen = {};
      }

      const visible = evaluateShowWhen(showWhen);
      group.classList.toggle('hidden', !visible);

      group.querySelectorAll('input, select, textarea').forEach((input) => {
        if (!visible) {
          input.removeAttribute('required');
        }
      });
    });
  }

  function validateField(group) {
    const input = group.querySelector('.form-input, select.form-input');
    const checkbox = group.querySelector('.toggle input, .checkbox-group');
    const errorEl = group.querySelector('.field-error');
    let valid = true;
    let message = '';

    if (group.classList.contains('hidden')) {
      if (errorEl) errorEl.textContent = '';
      return true;
    }

    if (input) {
      if (input.hasAttribute('required') && !input.value.trim()) {
        valid = false;
        message = '此字段为必填项';
      } else if (input.type === 'number') {
        const val = Number(input.value);
        const min = input.min !== '' ? Number(input.min) : null;
        const max = input.max !== '' ? Number(input.max) : null;
        if (min !== null && val < min) {
          valid = false;
          message = `最小值为 ${min}`;
        }
        if (max !== null && val > max) {
          valid = false;
          message = `最大值为 ${max}`;
        }
      } else if (input.pattern && input.value) {
        const regex = new RegExp(input.pattern);
        if (!regex.test(input.value)) {
          valid = false;
          message = '格式不正确';
        }
      }
      input.classList.toggle('invalid', !valid);
    }

    if (checkbox && group.querySelector('.checkbox-group')) {
      const name = group.dataset.question;
      const checked = form.querySelectorAll(`[name="${name}"]:checked`);
      if (group.querySelector('.required') && checked.length === 0) {
        valid = false;
        message = '请至少选择一项';
      }
    }

    if (errorEl) errorEl.textContent = message;
    return valid;
  }

  form.querySelectorAll('input, select').forEach((el) => {
    el.addEventListener('input', () => {
      updateVisibility();
      validateField(el.closest('.form-group'));
    });
    el.addEventListener('change', () => {
      updateVisibility();
      validateField(el.closest('.form-group'));
    });
  });

  form.addEventListener('submit', (e) => {
    updateVisibility();
    let allValid = true;
    form.querySelectorAll('.form-group').forEach((group) => {
      if (!validateField(group)) allValid = false;
    });
    if (!allValid) e.preventDefault();
  });

  updateVisibility();
});
