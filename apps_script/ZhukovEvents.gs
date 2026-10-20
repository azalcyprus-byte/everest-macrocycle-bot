/**
 * Everest / Alexey — optional source-event collector for existing Google Forms.
 * Bind this script to the existing Alexey response spreadsheet.
 * Add an installable spreadsheet "On form submit" trigger for onAlexeyFormSubmit.
 * Does not modify the existing forms or their response sheets.
 */
const EVEREST_EVENTS_TAB = 'Everest_Алексей_события';

function onAlexeyFormSubmit(e) {
  if (!e || !e.range) {
    throw new Error('An installable spreadsheet form-submit trigger is required.');
  }
  const source = e.range.getSheet();
  const tab = source.getName();
  if (tab !== 'Утро — ответы' && tab !== 'После игры — ответы') return;
  const row = e.range.getRow();
  const values = source.getRange(row, 1, 1, 3).getDisplayValues()[0];
  const stamp = values[0] || '';
  const date = values[1] || '';
  const eventKey = tab + ':' + row;
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const book = e.source || SpreadsheetApp.getActiveSpreadsheet();
    let sheet = book.getSheetByName(EVEREST_EVENTS_TAB);
    if (!sheet) {
      sheet = book.insertSheet(EVEREST_EVENTS_TAB);
      sheet.appendRow(['EVENT_KEY', 'SOURCE_TAB', 'REPORT_DATE', 'SUBMITTED_AT', 'RECORDED_AT']);
    }
    // Avoid duplicate events if a trigger is inadvertently installed twice.
    const last = sheet.getLastRow();
    if (last > 1) {
      const keys = sheet.getRange(2, 1, last - 1, 1).getDisplayValues();
      if (keys.some(x => x[0] === eventKey)) return;
    }
    sheet.appendRow([eventKey, tab, date, stamp, new Date()]);
  } finally {
    lock.releaseLock();
  }
}

/** One-time setup in the spreadsheet's Apps Script editor. */
function installAlexeyFormTrigger() {
  const handlers = ScriptApp.getProjectTriggers()
    .filter(x => x.getHandlerFunction() === 'onAlexeyFormSubmit');
  if (handlers.length) return;
  ScriptApp.newTrigger('onAlexeyFormSubmit')
    .forSpreadsheet(SpreadsheetApp.getActiveSpreadsheet())
    .onFormSubmit()
    .create();
}
