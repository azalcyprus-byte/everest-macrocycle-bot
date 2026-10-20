/**
 * Everest / Alexey — source-event collector for EXISTING Google Forms.
 * Bind this file to Alexey's response spreadsheet.
 * Neither live responses nor the curator's own forms are edited.
 */
const EVEREST_EVENTS_TAB = 'Everest_Алексей_события';

function appendAlexeyEvent_(book, eventKey, tab, reportDate, submittedAt) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    let sheet = book.getSheetByName(EVEREST_EVENTS_TAB);
    if (!sheet) {
      sheet = book.insertSheet(EVEREST_EVENTS_TAB);
      sheet.appendRow([
        'EVENT_KEY', 'SOURCE_TAB', 'REPORT_DATE', 'SUBMITTED_AT', 'RECORDED_AT'
      ]);
    }
    const last = sheet.getLastRow();
    if (last > 1) {
      const keys = sheet.getRange(2, 1, last - 1, 1).getDisplayValues();
      if (keys.some(row => row[0] === eventKey)) return false;
    }
    sheet.appendRow([eventKey, tab, reportDate, submittedAt, new Date()]);
    return true;
  } finally {
    lock.releaseLock();
  }
}

function onAlexeyFormSubmit(e) {
  if (!e || !e.range) {
    throw new Error('Installable spreadsheet On form submit event is required.');
  }
  const sourceSheet = e.range.getSheet();
  const tab = sourceSheet.getName();
  if (tab !== 'Утро — ответы' && tab !== 'После игры — ответы') return;

  const row = e.range.getRow();
  const values = sourceSheet.getRange(row, 1, 1, 3).getDisplayValues()[0];
  appendAlexeyEvent_(
    e.source || SpreadsheetApp.getActiveSpreadsheet(),
    tab + ':' + row,
    tab,
    values[1] || '',
    values[0] || ''
  );
}

/** Run once in the existing spreadsheet's Apps Script editor. */
function installAlexeyFormTrigger() {
  const present = ScriptApp.getProjectTriggers()
    .some(trigger => trigger.getHandlerFunction() === 'onAlexeyFormSubmit');
  if (present) return;
  ScriptApp.newTrigger('onAlexeyFormSubmit')
    .forSpreadsheet(SpreadsheetApp.getActiveSpreadsheet())
    .onFormSubmit()
    .create();
}

/**
 * Safe MANUAL self-test. Never submits a form or edits Alexey's reports.
 * 1) Checks that the form-submit trigger exists.
 * 2) Inserts a synthetic __TEST_ONLY__ event via the same journal writer.
 * 3) Verifies the record and deletes exactly the matching test row.
 * Result is visible in Apps Script → Execution log.
 */
function testAlexeyEventJournal() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  if (!book) throw new Error('Script must be bound to Alexey response spreadsheet.');

  const installed = ScriptApp.getProjectTriggers()
    .some(trigger => trigger.getHandlerFunction() === 'onAlexeyFormSubmit');
  if (!installed) throw new Error('Trigger onAlexeyFormSubmit not installed.');

  const testKey = 'TEST_ONLY:EVEREST:' + Utilities.getUuid();
  let created = false;
  try {
    created = appendAlexeyEvent_(
      book, testKey, '__TEST_ONLY__', '', 'SYNTHETIC_NOT_FORM_DATA'
    );
    if (!created) throw new Error('Unique test event was rejected.');
    const events = book.getSheetByName(EVEREST_EVENTS_TAB);
    const count = events.getLastRow() - 1;
    const matches = count > 0
      ? events.getRange(2, 1, count, 1)
          .createTextFinder(testKey).matchEntireCell(true).findAll()
      : [];
    if (matches.length !== 1) {
      throw new Error('Expected exactly one synthetic event: got ' + matches.length);
    }
    const row = matches[0].getRow();
    const values = events.getRange(row, 1, 1, 4).getDisplayValues()[0];
    if (values[1] !== '__TEST_ONLY__' || values[3] !== 'SYNTHETIC_NOT_FORM_DATA') {
      throw new Error('Unexpected synthetic event contents.');
    }
    Logger.log('PASS: form trigger exists and the isolated event writer works.');
  } finally {
    if (created) {
      const lock = LockService.getScriptLock();
      lock.waitLock(30000);
      try {
        const events = book.getSheetByName(EVEREST_EVENTS_TAB);
        const count = events.getLastRow() - 1;
        const matches = count > 0
          ? events.getRange(2, 1, count, 1)
              .createTextFinder(testKey).matchEntireCell(true).findAll()
          : [];
        if (matches.length !== 1) {
          throw new Error('Cleanup requires exactly one matching TEST_ONLY event.');
        }
        const row = matches[0].getRow();
        if (events.getRange(row, 2).getDisplayValue() !== '__TEST_ONLY__') {
          throw new Error('Cleanup aborted: target is not a synthetic test row.');
        }
        events.deleteRow(row);
        Logger.log('CLEANUP: synthetic test event removed; reports untouched.');
      } finally {
        lock.releaseLock();
      }
    }
  }
}
