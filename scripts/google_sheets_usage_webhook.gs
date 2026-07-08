function doPost(e) {
  const payload = JSON.parse(e.postData.contents || "{}");
  const values = payload.values || [];
  const eventId = payload.eventId || "";
  const cache = CacheService.getScriptCache();
  if (eventId && cache.get(eventId)) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: true, duplicate: true, rows: 0 }))
      .setMimeType(ContentService.MimeType.JSON);
  }
  const spreadsheetId = payload.spreadsheetId || "";
  const spreadsheet = spreadsheetId
    ? SpreadsheetApp.openById(spreadsheetId)
    : SpreadsheetApp.getActiveSpreadsheet();
  const sheetId = payload.sheetId == null ? "" : String(payload.sheetId);
  const sheetName = payload.sheetName || "Usage Cost";
  let sheet = null;

  if (sheetId) {
    sheet = spreadsheet.getSheets().find((candidate) => String(candidate.getSheetId()) === sheetId);
  }
  if (!sheet && sheetName) {
    sheet = spreadsheet.getSheetByName(sheetName);
  }
  if (!sheet) {
    sheet = spreadsheet.getSheetByName("Usage Cost") || spreadsheet.getActiveSheet();
  }

  if (!values.length) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: "No values supplied" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  sheet.getRange(sheet.getLastRow() + 1, 1, values.length, values[0].length).setValues(values);
  if (eventId) {
    cache.put(eventId, "1", 21600);
  }

  return ContentService
    .createTextOutput(JSON.stringify({ ok: true, rows: values.length }))
    .setMimeType(ContentService.MimeType.JSON);
}
