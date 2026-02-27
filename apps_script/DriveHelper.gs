/**
 * DriveHelper — Apps Script web app for creating Google Sheets/Docs
 * owned by the Drive owner (joe_tay@moe.edu.sg).
 *
 * Deploy:
 *  1. Go to script.google.com and create a new project
 *  2. Paste this code into Code.gs
 *  3. Deploy > New deployment > Web app
 *     - Execute as: Me (joe_tay@moe.edu.sg)
 *     - Who has access: Anyone
 *  4. Copy the deployment URL and set as APPS_SCRIPT_URL env var
 *  5. Set APPS_SCRIPT_SECRET to the same value as SECRET below
 */

// Change this to a secure random string and set the same value in APPS_SCRIPT_SECRET
var SECRET = "CHANGE_ME_TO_A_SECURE_RANDOM_STRING";

// Service account email — files are shared with this account so the bot can access them
var SERVICE_ACCOUNT_EMAIL = "YOUR_SERVICE_ACCOUNT_EMAIL@PROJECT.iam.gserviceaccount.com";

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);

    // Auth check
    if (payload.secret !== SECRET) {
      return jsonResponse_({ error: "Unauthorized" }, 401);
    }

    var action = payload.action;
    var name = payload.name;
    var folderId = payload.folderId;

    if (!action || !name || !folderId) {
      return jsonResponse_({ error: "Missing required fields: action, name, folderId" }, 400);
    }

    var result;
    if (action === "createSheet") {
      result = createSheet_(name, folderId);
    } else if (action === "createDoc") {
      result = createDoc_(name, folderId, payload.content || "");
    } else {
      return jsonResponse_({ error: "Unknown action: " + action }, 400);
    }

    return jsonResponse_(result, 200);

  } catch (err) {
    return jsonResponse_({ error: err.message }, 500);
  }
}

function createSheet_(name, folderId) {
  var ss = SpreadsheetApp.create(name);
  var file = DriveApp.getFileById(ss.getId());

  // Move to target folder
  var folder = DriveApp.getFolderById(folderId);
  folder.addFile(file);
  DriveApp.getRootFolder().removeFile(file);

  // Share with service account so the bot can read/write
  file.addEditor(SERVICE_ACCOUNT_EMAIL);

  return {
    fileId: ss.getId(),
    url: ss.getUrl(),
    name: name
  };
}

function createDoc_(name, folderId, content) {
  var doc = DocumentApp.create(name);

  if (content) {
    doc.getBody().setText(content);
    doc.saveAndClose();
  }

  var file = DriveApp.getFileById(doc.getId());

  // Move to target folder
  var folder = DriveApp.getFolderById(folderId);
  folder.addFile(file);
  DriveApp.getRootFolder().removeFile(file);

  // Share with service account so the bot can read/write
  file.addEditor(SERVICE_ACCOUNT_EMAIL);

  return {
    fileId: doc.getId(),
    url: doc.getUrl(),
    name: name
  };
}

function jsonResponse_(data, code) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}
