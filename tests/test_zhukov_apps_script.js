const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Sheet {
  constructor(name, rows = []) {
    this.name = name;
    this.rows = rows.map(row => [...row]);
  }
  getName() { return this.name; }
  getRow() { return this.row; }
  getSheet() { return this; }
  getLastRow() { return this.rows.length; }
  appendRow(values) { this.rows.push([...values]); }
  deleteRow(row) { this.rows.splice(row - 1, 1); }
  getRange(row, col, countRows = 1, countCols = 1) {
    const sheet = this;
    return {
      getRow: () => row,
      getDisplayValue: () => String(sheet.rows[row - 1]?.[col - 1] ?? ''),
      getDisplayValues: () => Array.from({length: countRows}, (_, i) =>
        Array.from({length: countCols}, (_, j) =>
          String(sheet.rows[row - 1 + i]?.[col - 1 + j] ?? ''))),
      createTextFinder: text => ({
        matchEntireCell: () => ({
          findAll: () => Array.from({length: countRows}, (_, i) => i + row)
            .filter(r => sheet.rows[r - 1]?.[col - 1] === text)
            .map(r => ({getRow: () => r}))
        })
      })
    };
  }
}

const form = new Sheet('Утро — ответы', [
  ['Отметка времени', 'Дата', 'Город'],
  ['20.10.2026 09:00:00', '20.10.2026', 'Москва']
]);
const book = {
  sheets: new Map([[form.name, form]]),
  getSheetByName(name) { return this.sheets.get(name) || null; },
  insertSheet(name) {
    const sheet = new Sheet(name);
    this.sheets.set(name, sheet);
    return sheet;
  }
};
const triggers = [];
const messages = [];
const context = {
  SpreadsheetApp: {getActiveSpreadsheet: () => book},
  ScriptApp: {
    getProjectTriggers: () => triggers,
    newTrigger: fn => ({
      forSpreadsheet: () => ({
        onFormSubmit: () => ({
          create: () => triggers.push({getHandlerFunction: () => fn})
        })
      })
    })
  },
  LockService: {getScriptLock: () => ({
    waitLock: () => {},
    releaseLock: () => {}
  })},
  Utilities: {getUuid: () => 'integration-test-uuid'},
  Logger: {log: line => messages.push(line)},
  Date
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('apps_script/ZhukovEvents.gs', 'utf8'), context);

context.installAlexeyFormTrigger();
context.installAlexeyFormTrigger();
assert.equal(triggers.length, 1, 'trigger installation must be idempotent');
context.testAlexeyEventJournal();
const events = book.getSheetByName('Everest_Алексей_события');
assert.equal(events.getLastRow(), 1, 'test event must be cleaned up');
assert.equal(form.getLastRow(), 2, 'real response rows must be unchanged');
assert.ok(messages.some(text => text.startsWith('PASS:')));
assert.ok(messages.some(text => text.startsWith('CLEANUP:')));

const event = {source:book,range: {
  getSheet:() => form,
  getRow:() => 2
}};
context.onAlexeyFormSubmit(event);
context.onAlexeyFormSubmit(event);
assert.equal(events.getLastRow(), 2, 'one real form event only');
assert.equal(events.rows[1][1], 'Утро — ответы');
assert.equal(events.rows[1][2], '20.10.2026');
context.testAlexeyEventJournal();
assert.equal(events.getLastRow(), 2, 'cleanup must preserve existing event');
assert.equal(form.getLastRow(), 2, 'no form responses changed');
console.log('Apps Script in-memory integration checks passed');
