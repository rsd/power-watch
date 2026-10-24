import QtQuick
import Qt.labs.folderlistmodel
import Quickshell.Io
import "Model.js" as Model

// One file in one directory, read only after a directory listing has said it
// is no larger than `maxBytes`. That listing is the byte cap BEFORE the read:
// a FileView has no limit of its own and materialises the whole file as one
// string (measured: a 200 MB current.json held 0.8-1.7 GB per bar and
// OOM-killed two bars at a 3 GB cap). The listing is a FolderListModel — a
// readdir + stat on Qt's worker thread, no process — and FileView only ever
// gets a path the listing has passed (Model.gateDecision).
//
// What the listing does (measured under a scratch qs): it follows an atomic
// rename (os.replace) with the new size, leaves FIFOs out (so a FIFO reads as
// missing), reports a symlink's target size, and lists /dev/zero as 0 bytes.
// A 0-byte entry is never read (Model.gateDecision says "missing"): procfs
// lists its generated files as 0 bytes too, and a current.json linked to
// /proc/kallsyms read 20 MB through the 16 KiB gate before that rule (with it:
// "missing", nothing read). An empty file has nothing to read anyway.
//
// How a change is learned of:
//   - the listing watches the directory, so a file replaced by rename (how the
//     service writes current.json and compacts the tiers), created or deleted
//     re-lists on its own — and so does any other file's rename in the same
//     directory, which is why reads are deduplicated and throttled below;
//   - an in-place append (the history tiers between compactions) does not
//     touch the directory. With `watchContent`, the FileView's own watch
//     reports it, and the directory is re-listed for a fresh size first;
//   - relist() re-reads the directory, and re-arms its watch, which never arms
//     on a directory that does not exist yet (the owner's staleness tick calls
//     it while the outcome is "missing").
// A version (size + mtime) already handed to the reader is not read again,
// and with `minIntervalMs` a read due inside the window waits for its end,
// then re-lists so it reads the latest size: at most one read per window.
//
// Residual: a file that grows between the listing and FileView's open is read
// whole (TOCTOU), and so is one whose size the listing gets wrong without
// saying 0 (a FUSE file can report any size; sysfs binary attributes report
// their real bound). Both files are written by the user's own service, in the
// user's state dir.
Item {
  id: gate

  property string dir: ""
  property string name: ""
  property real maxBytes: 0
  property bool watchContent: false
  property int minIntervalMs: 0

  // pending / ok / missing / too large / unreadable
  property string outcome: "pending"
  // The version handed to the FileView last; "" = none.
  property string version: ""
  property bool waiting: false
  property bool resetting: false
  property bool ready: false

  readonly property string filePath: gate.dir + "/" + gate.name

  signal textRead(string content)
  signal failed(string outcome)

  function relist() {
    if (gate.dir === "" || gate.name === "") return
    gate.resetting = true
    listing.folder = ""
    listing.folder = Model.fileUrl(gate.dir)
    gate.resetting = false
    // An existing directory moves the listing to Loading, then Ready, a moment
    // later (asynchronously: the listing is Null right after the assignment
    // either way). One that does not exist leaves it Null with no signal at
    // all; nullCheck catches that case.
    nullCheck.restart()
  }

  // The content watch fired: re-list now, or once the read window closes.
  function poke() {
    if (throttle.running) {
      gate.waiting = true
      return
    }
    gate.relist()
  }

  function entry() {
    for (var i = 0; i < listing.count; i++) {
      if (listing.get(i, "fileName") !== gate.name) continue
      var modified = listing.get(i, "fileModified")
      return {
        size: listing.get(i, "fileSize"),
        modified: modified ? modified.getTime() : null,
        isDir: listing.get(i, "fileIsDir")
      }
    }
    return null
  }

  function fail(outcome) {
    gate.outcome = outcome
    gate.failed(outcome)
  }

  function settle() {
    var decision = Model.gateDecision(gate.entry(), gate.maxBytes, gate.version)
    if (decision.action === "none") return
    if (decision.action === "read") {
      if (throttle.running) {
        gate.waiting = true
        return
      }
      gate.version = decision.version
      if (file.path === gate.filePath) file.reload()
      else file.path = gate.filePath
      if (gate.minIntervalMs > 0) throttle.restart()
      return
    }
    // Nothing to read: drop the path, so the FileView holds no text and no
    // watch on a file it must not read.
    gate.version = ""
    file.path = ""
    // Said once per change of outcome, not on every re-list.
    if (gate.outcome === decision.action) return
    gate.fail(decision.action)
  }

  function restart() {
    throttle.stop()
    gate.waiting = false
    gate.version = ""
    gate.outcome = "pending"
    file.path = ""
    gate.relist()
  }

  onDirChanged: if (gate.ready) Qt.callLater(gate.restart)
  onNameChanged: if (gate.ready) Qt.callLater(gate.restart)
  Component.onCompleted: {
    gate.ready = true
    gate.restart()
  }

  FolderListModel {
    id: listing
    nameFilters: [gate.name]
    showDirs: false
    showDotAndDotDot: false
    onStatusChanged: {
      if (gate.resetting || listing.status === FolderListModel.Loading) return
      // Ready: a fresh listing. Null outside relist(): the directory went away.
      nullCheck.stop()
      gate.settle()
    }
  }

  // Single-shot: still Null this long after relist() = no such directory.
  Timer {
    id: nullCheck
    interval: 1000
    repeat: false
    onTriggered: if (listing.status === FolderListModel.Null) gate.settle()
  }

  // Single-shot read window; the last change inside it is caught up at its end.
  Timer {
    id: throttle
    interval: Math.max(1, gate.minIntervalMs)
    repeat: false
    onTriggered: {
      if (!gate.waiting) return
      gate.waiting = false
      gate.relist()
    }
  }

  FileView {
    id: file
    path: ""
    watchChanges: gate.watchContent
    printErrors: false
    onFileChanged: gate.poke()
    onLoaded: {
      gate.outcome = "ok"
      gate.textRead(file.text())
    }
    onLoadFailed: function(error) {
      if (file.path === "") return
      var outcome = Model.loadFailure(error === FileViewError.FileNotFound)
      // A file gone between listing and open is read again once it is back.
      if (outcome === "missing") gate.version = ""
      gate.fail(outcome)
    }
  }
}
