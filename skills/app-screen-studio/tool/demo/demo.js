// 製品側の動作の例: 編集モードでは押せず、操作確認モードでは動くことを確かめる
var n = 0;
document.getElementById('countBtn').addEventListener('click', function () {
  n += 1;
  this.textContent = '記録する (' + n + ')';
  document.getElementById('result').textContent = '記録しました';
});
document.getElementById('memoForm').addEventListener('submit', function (e) {
  e.preventDefault();
  document.getElementById('result').textContent = '送信しました(デモ)';
});
