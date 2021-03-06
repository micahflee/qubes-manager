#!/usr/bin/python2
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2012  Agnieszka Kostrzewa <agnieszka.kostrzewa@gmail.com>
# Copyright (C) 2012  Marek Marczykowski <marmarek@mimuw.edu.pl>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with this program; if not, see <http://www.gnu.org/licenses/>.
#
#

import sys
from PyQt4 import QtCore  # pylint: disable=import-error
from PyQt4 import QtGui  # pylint: disable=import-error
import threading
import time
import os
import os.path
import traceback
import logging
import logging.handlers

import signal

from qubes import backup

from . import ui_restoredlg  # pylint: disable=no-name-in-module
from . import multiselectwidget
from . import backup_utils
from . import thread_monitor

from multiprocessing import Queue, Event
from multiprocessing.queues import Empty
from qubesadmin import Qubes, exc
from qubesadmin.backup import restore


class RestoreVMsWindow(ui_restoredlg.Ui_Restore, QtGui.QWizard):

    def __init__(self, qt_app, qubes_app, parent=None):
        super(RestoreVMsWindow, self).__init__(parent)

        self.qt_app = qt_app
        self.qubes_app = qubes_app

        self.vms_to_restore = None
        self.func_output = []

        # Set up logging
        self.feedback_queue = Queue()
        handler = logging.handlers.QueueHandler(self.feedback_queue)
        logger = logging.getLogger('qubesadmin.backup')
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        self.canceled = False
        self.error_detected = Event()
        self.thread_monitor = None
        self.backup_restore = None
        self.target_appvm = None

        self.setupUi(self)

        self.select_vms_widget = multiselectwidget.MultiSelectWidget(self)
        self.select_vms_layout.insertWidget(1, self.select_vms_widget)

        self.connect(self,
                     QtCore.SIGNAL("currentIdChanged(int)"),
                     self.current_page_changed)
        self.dir_line_edit.connect(self.dir_line_edit,
                                   QtCore.SIGNAL("textChanged(QString)"),
                                   self.backup_location_changed)

        self.select_dir_page.isComplete = self.has_selected_dir
        self.select_vms_page.isComplete = self.has_selected_vms
        self.confirm_page.isComplete = self.all_vms_good
        # FIXME
        # this causes to run isComplete() twice, I don't know why
        self.select_vms_page.connect(
            self.select_vms_widget,
            QtCore.SIGNAL("selected_changed()"),
            QtCore.SIGNAL("completeChanged()"))

        backup_utils.fill_appvms_list(self)

    @QtCore.pyqtSlot(name='on_select_path_button_clicked')
    def select_path_button_clicked(self):
        backup_utils.select_path_button_clicked(self, True)

    def cleanupPage(self, p_int):  # pylint: disable=invalid-name
        if self.page(p_int) is self.select_vms_page:
            self.vms_to_restore = None
        else:
            super(RestoreVMsWindow, self).cleanupPage(p_int)

    def __fill_vms_list__(self):
        if self.vms_to_restore is not None:
            return

        self.select_vms_widget.selected_list.clear()
        self.select_vms_widget.available_list.clear()

        self.target_appvm = None
        if self.appvm_combobox.currentIndex() != 0:   # An existing appvm chosen
            self.target_appvm = self.qubes_app.domains[
                str(self.appvm_combobox.currentText())]

        try:
            self.backup_restore = restore.BackupRestore(
                self.qubes_app,
                self.dir_line_edit.text(),
                self.target_appvm,
                self.passphrase_line_edit.text()
            )

            if self.ignore_missing.isChecked():
                self.backup_restore.options.use_default_template = True
                self.backup_restore.options.use_default_netvm = True

            if self.ignore_uname_mismatch.isChecked():
                self.backup_restore.options.ignore_username_mismatch = True

            if self.verify_only.isChecked():
                self.backup_restore.options.verify_only = True

            # pylint: disable=assignment-from-no-return
            self.vms_to_restore = self.backup_restore.get_restore_info()

            for vmname in self.vms_to_restore:
                if vmname.startswith('$'):
                    # Internal info
                    continue
                self.select_vms_widget.available_list.addItem(vmname)
        except exc.QubesException as ex:
            QtGui.QMessageBox.warning(None, self.tr("Restore error!"), str(ex))

    def append_output(self, text):
        self.commit_text_edit.append(text)

    def __do_restore__(self, t_monitor):
        err_msg = []
        try:
            self.backup_restore.restore_do(self.vms_to_restore)

        except backup.BackupCanceledError as ex:
            self.canceled = True
            err_msg.append(str(ex))
        except Exception as ex:  # pylint: disable=broad-except
            err_msg.append(str(ex))
            err_msg.append(
                self.tr("Partially restored files left in /var/tmp/restore_*, "
                        "investigate them and/or clean them up"))

        if self.canceled:
            self.append_output('<b><font color="red">{0}</font></b>'.format(
                self.tr("Restore aborted!")))
        elif err_msg or self.error_detected.is_set():
            if err_msg:
                t_monitor.set_error_msg('\n'.join(err_msg))
            self.append_output('<b><font color="red">{0}</font></b>'.format(
                self.tr("Finished with errors!")))
        else:
            self.append_output('<font color="green">{0}</font>'.format(
                self.tr("Finished successfully!")))

        t_monitor.set_finished()

    def current_page_changed(self, page_id):  # pylint: disable=unused-argument

        old_sigchld_handler = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        if self.currentPage() is self.select_vms_page:
            self.__fill_vms_list__()

        elif self.currentPage() is self.confirm_page:
            # pylint: disable=assignment-from-no-return
            self.vms_to_restore = self.backup_restore.get_restore_info()

            for i in range(self.select_vms_widget.available_list.count()):
                vmname = self.select_vms_widget.available_list.item(i).text()
                del self.vms_to_restore[str(vmname)]

            self.vms_to_restore = self.backup_restore.restore_info_verify(
                self.vms_to_restore)

            self.func_output = self.backup_restore.get_restore_summary(
                self.vms_to_restore
            )

            self.confirm_text_edit.setReadOnly(True)
            self.confirm_text_edit.setFontFamily("Monospace")
            self.confirm_text_edit.setText(self.func_output)

            self.confirm_page.emit(QtCore.SIGNAL("completeChanged()"))

        elif self.currentPage() is self.commit_page:
            self.button(self.FinishButton).setDisabled(True)
            self.showFileDialog.setEnabled(True)
            self.showFileDialog.setChecked(self.showFileDialog.isEnabled()
                                           and str(self.dir_line_edit.text())
                                           .count("media/") > 0)

            self.thread_monitor = thread_monitor.ThreadMonitor()
            thread = threading.Thread(target=self.__do_restore__,
                                      args=(self.thread_monitor,))
            thread.daemon = True
            thread.start()
            while not self.thread_monitor.is_finished():
                self.qt_app.processEvents()
                time.sleep(0.1)
                try:
                    log_record = self.feedback_queue.get_nowait()
                    while log_record:
                        if log_record.levelno == logging.ERROR or\
                                        log_record.levelno == logging.CRITICAL:
                            output = '<font color="red">{0}</font>'.format(
                                log_record.getMessage())
                        else:
                            output = log_record.getMessage()
                        self.append_output(output)
                        log_record = self.feedback_queue.get_nowait()
                except Empty:
                    pass

            if not self.thread_monitor.success:
                if not self.canceled:
                    QtGui.QMessageBox.warning(
                        None,
                        self.tr("Backup error!"),
                        self.tr("ERROR: {0}").format(
                            self.thread_monitor.error_msg))
            self.progress_bar.setMaximum(100)
            self.progress_bar.setValue(100)

            if self.showFileDialog.isChecked():
                self.append_output(
                    '<b><font color="black">{0}</font></b>'.format(
                        self.tr("Please unmount your backup volume and cancel "
                                "the file selection dialog.")))
                self.qt_app.processEvents()
                backup_utils.select_path_button_clicked(self, False, True)

            self.button(self.FinishButton).setEnabled(True)
            self.button(self.CancelButton).setEnabled(False)
            self.showFileDialog.setEnabled(False)

        signal.signal(signal.SIGCHLD, old_sigchld_handler)

    def all_vms_good(self):
        for vm_info in self.vms_to_restore.values():
            if not vm_info.vm:
                continue
            if not vm_info.good_to_go:
                return False
        return True

    def reject(self):
        if self.currentPage() is self.commit_page:
            self.backup_restore.canceled = True
            self.append_output('<font color="red">{0}</font>'.format(
                self.tr("Aborting the operation...")))
            self.button(self.CancelButton).setDisabled(True)
        else:
            self.done(0)

    def has_selected_dir(self):
        backup_location = self.dir_line_edit.text()
        if not backup_location:
            return False
        if self.appvm_combobox.currentIndex() == 0:
            if os.path.isfile(backup_location) or \
                    os.path.isfile(os.path.join(backup_location, 'qubes.xml')):
                return True
        else:
            return True

        return False

    def has_selected_vms(self):
        return self.select_vms_widget.selected_list.count() > 0

    def backup_location_changed(self, new_dir=None):
        # pylint: disable=unused-argument
        self.select_dir_page.emit(QtCore.SIGNAL("completeChanged()"))


# Bases on the original code by:
# Copyright (c) 2002-2007 Pascal Varet <p.varet@gmail.com>

def handle_exception(exc_type, exc_value, exc_traceback):

    filename, line, dummy, dummy = traceback.extract_tb(exc_traceback).pop()
    filename = os.path.basename(filename)
    error = "%s: %s" % (exc_type.__name__, exc_value)

    QtGui.QMessageBox.critical(None, "Houston, we have a problem...",
                         "Whoops. A critical error has occured. "
                         "This is most likely a bug "
                         "in Qubes Restore VMs application.<br><br>"
                         "<b><i>%s</i></b>" % error +
                         "at <b>line %d</b> of file <b>%s</b>.<br/><br/>"
                                      % (line, filename))


def main():

    qt_app = QtGui.QApplication(sys.argv)
    qt_app.setOrganizationName("The Qubes Project")
    qt_app.setOrganizationDomain("http://qubes-os.org")
    qt_app.setApplicationName("Qubes Restore VMs")

    sys.excepthook = handle_exception

    qubes_app = Qubes()

    restore_window = RestoreVMsWindow(qt_app, qubes_app)

    restore_window.show()

    qt_app.exec_()
    qt_app.exit()


if __name__ == "__main__":
    main()
