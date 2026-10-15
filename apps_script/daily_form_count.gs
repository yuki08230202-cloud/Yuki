/**
 * 毎日の企業フォーム送付数を「企業フォーム送付_記録」タブへ自動記録する。
 *
 * 数え方（「フォーム営業リスト」タブ）
 *   第1波（1次打診）: 打診日 が今日 かつ 打診完了 が TRUE の行数
 *   第2波（2次打診）: 2次打診日 が今日 かつ 2次打診完了 が TRUE の行数（2次打診をした日だけ記録）
 *
 * 記録先（「企業フォーム送付_記録」タブ）
 *   第1波: A列=日付 / B列=送付数 / C列=累計
 *   第2波: F列=日付 / G列=送付数 / H列=累計
 *   同じ日付の行がすでにあれば上書きし、なければ一番下の次の行に追加する。
 *
 * 初回だけ: setupDailyTrigger を実行すると、毎日18時台に recordTodayFormCount が自動で動く。
 */

const LIST_SHEET = 'フォーム営業リスト';
const RECORD_SHEET = '企業フォーム送付_記録';
const FIRST_DATA_ROW = 6; // 記録タブで日付が始まる行
const TZ = 'Asia/Tokyo';

function recordTodayFormCount() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const today = new Date();
  const counts = countToday_(ss.getSheetByName(LIST_SHEET), today);

  const record = ss.getSheetByName(RECORD_SHEET);
  const dateText = Utilities.formatDate(today, TZ, 'yyyy/MM/dd');
  writeRow_(record, 1, dateText, counts.first); // 第1波: A〜C列
  if (counts.second > 0) {
    writeRow_(record, 6, dateText, counts.second); // 第2波: F〜H列
  }
  Logger.log('%s 第1波=%s 第2波=%s', dateText, counts.first, counts.second);
}

function countToday_(sheet, today) {
  const values = sheet.getDataRange().getValues();
  let headerRow = -1;
  for (let r = 0; r < Math.min(10, values.length); r++) {
    if (values[r].indexOf('打診日') !== -1) { headerRow = r; break; }
  }
  if (headerRow < 0) throw new Error('「打診日」の見出しが見つかりません');
  const h = values[headerRow];
  const col = name => {
    const i = h.indexOf(name);
    if (i < 0) throw new Error('見出し「' + name + '」が見つかりません');
    return i;
  };
  const c1Date = col('打診日'), c1Done = col('打診完了');
  const c2Date = col('2次打診日'), c2Done = col('2次打診完了');

  let first = 0, second = 0;
  for (let r = headerRow + 1; r < values.length; r++) {
    const row = values[r];
    if (isSameDay_(row[c1Date], today) && row[c1Done] === true) first++;
    if (isSameDay_(row[c2Date], today) && row[c2Done] === true) second++;
  }
  return { first: first, second: second };
}

// 日付セル（Date型）と「10/15」「2026/10/15」などの文字列の両方に対応
function isSameDay_(cell, today) {
  if (cell === '' || cell === null) return false;
  const y = Number(Utilities.formatDate(today, TZ, 'yyyy'));
  const m = Number(Utilities.formatDate(today, TZ, 'M'));
  const d = Number(Utilities.formatDate(today, TZ, 'd'));
  if (cell instanceof Date) {
    return Utilities.formatDate(cell, TZ, 'yyyy/M/d') === y + '/' + m + '/' + d;
  }
  const parts = String(cell).trim().split(/[\/\-.月日]/).filter(String).map(Number);
  if (parts.length === 2) return parts[0] === m && parts[1] === d;
  if (parts.length === 3) return parts[0] === y && parts[1] === m && parts[2] === d;
  return false;
}

// startCol: 1 なら A〜C列、6 なら F〜H列
function writeRow_(sheet, startCol, dateText, count) {
  const lastRow = Math.max(sheet.getLastRow(), FIRST_DATA_ROW);
  const dates = sheet.getRange(FIRST_DATA_ROW, startCol, lastRow - FIRST_DATA_ROW + 1, 1).getDisplayValues();
  let target = -1, lastFilled = FIRST_DATA_ROW - 1;
  for (let i = 0; i < dates.length; i++) {
    const v = dates[i][0];
    if (v !== '') lastFilled = FIRST_DATA_ROW + i;
    if (v === dateText) target = FIRST_DATA_ROW + i;
  }
  if (target < 0) target = lastFilled + 1;

  sheet.getRange(target, startCol).setValue(dateText);
  sheet.getRange(target, startCol + 1).setValue(count);
  // 累計: 数式が入っていればそのまま（自動計算）、空なら前の行の累計＋今日の送付数
  const total = sheet.getRange(target, startCol + 2);
  if (!total.getFormula()) {
    const prev = target > FIRST_DATA_ROW ? Number(sheet.getRange(target - 1, startCol + 2).getValue()) || 0 : 0;
    total.setValue(prev + count);
  }
}

function setupDailyTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'recordTodayFormCount')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('recordTodayFormCount').timeBased().everyDays(1).atHour(18).inTimezone(TZ).create();
}
