/* 确认卡片:替代浏览器原生 confirm()。
 *
 * 原生对话框在页面上方弹出、样式无法控制,与工作台风格不一致。
 * 这里拦截带 data-confirm 的表单提交,居中显示一张与 DESIGN.md 同款的卡片。
 */
(function () {
  var box = null;

  function close() {
    if (box) { box.remove(); box = null; }
    document.removeEventListener('keydown', onKey);
  }

  function onKey(e) {
    if (!box) return;
    if (e.key === 'Escape') { close(); }
    if (e.key === 'Enter') {
      var ok = box.querySelector('[data-ok]');
      if (ok) { ok.click(); }
    }
  }

  function open(form, text) {
    close();
    box = document.createElement('div');
    box.className = 'confirmwrap';
    box.innerHTML =
      '<div class="confirmbox" role="alertdialog" aria-modal="true" aria-labelledby="cfTitle">' +
      '<h3 id="cfTitle">确认操作</h3>' +
      '<p class="confirmtext"></p>' +
      '<div class="actions">' +
      '<button type="button" class="danger" data-ok>确认</button>' +
      '<button type="button" class="ghost" data-cancel>取消</button>' +
      '</div></div>';
    box.querySelector('.confirmtext').textContent = text;
    document.body.appendChild(box);

    box.querySelector('[data-ok]').addEventListener('click', function () {
      form.dataset.confirmed = '1';
      close();
      // 用原生 submit 绕过 submit 事件,避免再次被拦截
      HTMLFormElement.prototype.submit.call(form);
    });
    box.querySelector('[data-cancel]').addEventListener('click', close);
    box.addEventListener('click', function (e) { if (e.target === box) { close(); } });
    document.addEventListener('keydown', onKey);
    box.querySelector('[data-ok]').focus();
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.dataset.confirm || form.dataset.confirmed === '1') return;
    e.preventDefault();
    open(form, form.dataset.confirm);
  }, true);
})();
