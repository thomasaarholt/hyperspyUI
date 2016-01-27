# -*- coding: utf-8 -*-
# Copyright 2007-2016 The HyperSpyUI developers
#
# This file is part of HyperSpyUI.
#
# HyperSpyUI is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# HyperSpyUI is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with HyperSpyUI.  If not, see <http://www.gnu.org/licenses/>.
"""
Created on Tue Nov 04 13:37:08 2014

@author: Vidar Tonaas Fauske
"""

import os
import gc
import re
import numpy as np
import logging
import warnings
import traceback
import sys

# Hyperspy uses traitsui, set proper backend
from traits.etsconfig.api import ETSConfig
ETSConfig.toolkit = 'qt4'

from mainwindowutillayer import MainWindowActionRecorder, tr

import uiprogressbar
uiprogressbar.takeover_progressbar()    # Enable hooks
import hooksignal
hooksignal.hook_signal()

from python_qt_binding import QtGui, QtCore
from QtCore import *
from QtGui import *
from hyperspyui.signalwrapper import SignalWrapper
from hyperspyui.bindinglist import BindingList
from hyperspyui.widgets.dataviewwidget import DataViewWidget
from hyperspyui.widgets.editorwidget import EditorWidget
import hyperspyui.util
from hyperspyui.mdi_mpl_backend import FigureCanvas

import hyperspy.io
import hyperspy.defaults_parser
from hyperspy.io_plugins import io_plugins

import overrides
overrides.override_hyperspy()           # Enable hyperspy overrides


glob_escape = re.compile(r'([\[\]])')
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class TrackEventFilter(QObject):
    """Qt Event filter for tracking the mouse position in the application.
    """

    track = Signal(QPoint)

    def eventFilter(self, receiver, event):
        if(event.type() == QEvent.MouseMove):
            self.track.emit(event.globalPos())
        # Call Base Class Method to Continue Normal Event Processing
        return False


class MainWindowHyperspy(MainWindowActionRecorder):

    """
    Fifth layer in the application stack. Should integrate hyperspy basics,
    such as UI wrappings for hyperspy classes (Signal and Model), file I/O,
    etc.
    """

    def __init__(self, parent=None):
        # Setup signals list. This is a BindingList, and all components of the
        # code that needs to keep track of the signals loaded bind into this.
        self.signals = BindingList()
        self.signals.add_custom(self._sweeper, None, None, None, self._sweeper,
                                None)
        self.hspy_signals = []

        def rem(x):
            # Needed since hyperspy has special equality operator...
            for i, s in enumerate(self.hspy_signals):
                if s is x.signal:
                    self.hspy_signals.pop(i)
        self.signals.add_custom(
            'hspy_signals',
            lambda x: self.hspy_signals.append(x.signal),
            lambda x, y: self.hspy_signals.insert(x, y.signal),
            None,
            rem,
            lambda x: self.hspy_signals.pop(x))
        self.lut_signalwrapper = dict()

        def lut_add(sw):
            self.lut_signalwrapper[sw.signal] = sw
        lut = self.lut_signalwrapper
        self.signals.add_custom('lut', lut_add, None, None,
                                lambda sw: lut.pop(sw.signal), None)

        # Setup variables
        self.progressbars = {}
        self.prev_mdi = None
        self._plotting_signal = None

        # Call super init, which creates main controls etc.
        super(MainWindowHyperspy, self).__init__(parent)

        self.create_statusbar()

        # Enable drag and drop
        self.setAcceptDrops(True)

        # Connect UIProgressBar for graphical hyperspy progress
        s = uiprogressbar.signaler
        s.connect(s, SIGNAL('created(int, int, QString)'),
                  self.on_progressbar_wanted)
        s.connect(s, SIGNAL('progress(int, int)'),
                  self.on_progressbar_update)
        s.connect(s, SIGNAL('progress(int, int, QString)'),
                  self.on_progressbar_update)
        s.connect(s, SIGNAL('finished_sig(int)'),
                  self.on_progressbar_finished)
        self.cancel_progressbar.connect(s.on_cancel)

        # Connect to Signal.plot events
        hooksignal.connect_plotting(self.on_signal_plotting)
        hooksignal.connect_plotted(self.on_signal_plotted)

        # Finish off hyperspy customization of layer 1
        self.setWindowTitle("HyperSpy")

    def _sweeper(self, removed):
        """
        Trigger a GC one second after calling this.
        """
        del removed
        QTimer.singleShot(1000, gc.collect)

    def create_widgetbar(self):
        # Add DataViewWidget to widget bar:
        self.tree = DataViewWidget(self, self)
        self.tree.setWindowTitle(tr("Data View"))
        # Sync tree with signals list:
        self.signals.add_custom(self.tree, self.tree.add_signal, None,
                                None, self.tree.remove, None)
        self.main_frame.subWindowActivated.connect(
            self.tree.on_mdiwin_activated)
        self.add_widget(self.tree)

        # Put other widgets at end (plugin widgets)
        super(MainWindowHyperspy, self).create_widgetbar()

    def create_menu(self):
        # Super creates Windows menu
        super(MainWindowHyperspy, self).create_menu()

        # Add custom action to signals' BindingList, so appropriate menu items
        # are removed if a signal is removed from the list
        def rem_s(value):
            for f in value.figures:
                self.windowmenu.removeAction(f.activateAction())
        self.signals.add_custom(self.windowmenu, None, None, None,
                                rem_s, lambda i: rem_s(self.signals[i]))

    def create_statusbar(self):
        """
        Creates extra status bar controls, e.g. coordinate tracking.
        """
        sb = self.statusBar()
        self.nav_coords_label = QLabel("Navigation: ()")
        sb.addPermanentWidget(self.nav_coords_label)
        self.mouse_coords_label = QLabel("Mouse: (,) px; (,)")
        sb.addPermanentWidget(self.mouse_coords_label)

        # To be able to update coordinates, we need to track the mouse
        # position with an event filter
        self.main_frame.subWindowActivated.connect(
            self._connect_figure_2_statusbar)
        app = QApplication.instance()
        self.tracker = TrackEventFilter()
        self.tracker.track.connect(self._on_track)
        app.installEventFilter(self.tracker)

    def _connect_figure_2_statusbar(self, mdi_window):
        """
        When a figure is activated, this callback sets the navigation status
        bar, and connects _on_active_navigate to the signal's AxesManager.
        """
        if mdi_window is self.prev_mdi:
            return
        s = hyperspyui.util.win2sig(mdi_window)
        if self.prev_mdi is not None:
            ps = hyperspyui.util.win2sig(self.prev_mdi, self.signals)
        else:
            ps = None
        if s is not ps:
            # If previous signal present, try to disconnect
            if ps and ps.signal and ps.signal.axes_manager:
                try:
                    ps.signal.axes_manager.events.indices_changed.disconnect(
                        self._on_active_navigate)
                except ValueError:
                    pass
            if s and s.signal and s.signal.axes_manager:
                try:
                    s.signal.axes_manager.events.indices_changed.connect(
                        self._on_active_navigate)
                except ValueError:
                    pass
                self.prev_mdi = mdi_window
                self._on_active_navigate(s.signal.axes_manager)

    def _on_active_navigate(self, axes_manager):
        """
        Callback triggered when the active signal navigates. Updates the
        status bar with the navigation indices.
        """
        ind = axes_manager.indices
        self.set_navigator_coords_status(ind)

    def _on_track(self, gpos):
        """
        Tracks the mouse position for the entire application, and if the mouse
        is over a figure axes it updates the status bar mouse coordinates.
        """

        # Find which window the mouse is above
        pos = self.mapFromGlobal(gpos)
        canvas = self.childAt(pos)
        # We only care about FigureCanvases
        if isinstance(canvas, FigureCanvas):
            fig = canvas.figure
            s, p = hyperspyui.util.fig2sig(fig, self.signals)
            # Currently we only know how to deal with standard plots
            if p is None:
                return
        else:
            return
        if p.ax is None:
            return
        # Map position to canvas frame of reference
        cpos = canvas.mapFromGlobal(gpos)
        # Mapping copied from MPL backend code:
        cpos = np.array([(cpos.x(), canvas.figure.bbox.height - cpos.y())])
        # Check that we are within plot axes
        (xa, ya), = p.ax.transAxes.inverted().transform(cpos)
        if not (0 <= xa <= 1 and 0 <= ya <= 1):
            return
        # Find coordinate values:
        (xd, yd), = p.ax.transData.inverted().transform(cpos)

        def v2i(a, v):
            if v < a.low_value:
                return a.low_index
            elif v > a.high_value:
                return a.high_index
            return a.value2index(v)

        if hasattr(p, 'axis'):                              # SpectrumFigure
            if p is s.signal._plot.navigator_plot:
                axis = p.axes_manager.navigation_axes[0]
            elif p is s.signal._plot.signal_plot:
                axis = p.axes_manager.signal_axes[0]
            else:
                return
            vals = (xd,)
            ind = (v2i(axis, xd),)
            units = [axis.units]
            intensity = yd
        elif hasattr(p, 'xaxis') and hasattr(p, 'yaxis'):   # ImagePlot
            vals = (xd, yd)
            ind = (v2i(p.xaxis, xd),
                   v2i(p.yaxis, yd))
            units = [p.xaxis.units, p.yaxis.units]
            intensity = p.ax.images[0].get_array()[ind[1], ind[0]]

        # Remove <undefined> units
        for i in xrange(len(units)):
            if unicode(units[i]) == u"<undefined>":
                units[i] = ""
        units = tuple(units)

        # Finally, display coordinates
        self.set_mouse_coords_status(ind, vals, units, intensity)

    def set_navigator_coords_status(self, coords):
        """
        Displays 'coords' as the navigator coordinates.
        """
        self.nav_coords_label.setText("Navigation: " + str(coords))

    def set_mouse_coords_status(self, indices, values, units, intensity=None):
        """
        Display mouse coordinates both in indices and data space values.

        'units' must be the same size as 'values'
        """
        vu = tuple([u"%.3g %s" % (v, u) for v, u in zip(values, units)])
        vu = "(%s)" % ", ".join(vu)
        text = "Mouse: " + str(indices) + " px; " + vu
        if intensity is not None:
            text += "; Intensity: "
            if isinstance(intensity, np.ndarray):
                text += str(intensity)
            else:
                text += "%.3g" % intensity
        self.mouse_coords_label.setText(text)

    def add_model(self, signal=None, *args, **kwargs):
        """
        Add a default model for the given/selected signal. Returns the
        newly created ModelWrapper.
        """
        if signal is None:
            signal = self.get_selected_wrapper()
        elif not isinstance(signal, SignalWrapper):
            signal = [s for s in self.signals if s.signal is signal]
            signal = signal[0]
        mw = signal.make_model(*args, **kwargs)
        return mw

    def make_component(self, comp_type):
        m = self.get_selected_model_wrapper()
        m.add_component(comp_type)

    def edit_hspy_settings(self):
        hyperspy.api.preferences.gui()

    # -------- Signal plotting callbacks -------
    def on_signal_plotting(self, signal, *args, **kwargs):
        # Check if we have a wrapper, if not we make one:
        if signal in self.lut_signalwrapper:
            # Replotting, make sure we keep it when closing
            sw = self.lut_signalwrapper[signal]
            sw.keep_on_close = True
        else:
            # New signal, make wrapper and add to list
            sw = SignalWrapper(signal, self)
            self.signals.append(sw)
        self._plotting_signal = sw

    def on_signal_plotted(self, signal, *args, **kwargs):
        sw = self.lut_signalwrapper[signal]
        sw.update_figures()
        if sw.keep_on_close:
            sw.keep_on_close = False
        self._plotting_signal = None
        self.main_frame.subWindowActivated.emit(
            self.main_frame.activeSubWindow())

    # -------- Selection management -------

    def get_selected_wrapper(self, error_on_multiple=False):
        signals = self.get_selected_wrappers()
        if signals is None or len(signals) < 1:
            return None
        elif len(signals) == 1:
            return signals[0]
        else:
            if error_on_multiple:
                mb = QMessageBox(QMessageBox.Information,
                                 tr("Select one signal only"),
                                 tr("You can only select one signal at the " +
                                     "time for this function. Currently, " +
                                     "several are selected"),
                                 QMessageBox.Ok)
                mb.exec_()
                raise RuntimeError()
            w = self.main_frame.activeSubWindow()
            s = [hyperspyui.util.win2sig(w, self.signals)]
            if s in signals:
                return s
            else:
                return signals[0]

    def get_selected_wrappers(self):
        s = self.tree.get_selected_wrappers()
        if len(s) < 1:
            w = self.main_frame.activeSubWindow()
            s = [hyperspyui.util.win2sig(w, self.signals)]
        return s

    def get_selected_signals(self):
        return [s.signal for s in self.get_selected_wrappers()]

    def get_selected_signal(self):
        sw = self.get_selected_wrapper()
        if sw is None:
            return None
        else:
            return sw.signal

    def get_selected_model_wrapper(self):
        """
        Returns the selected model
        """
        return self.tree.get_selected_model()

    def get_selected_model(self):
        """
        Returns the selected model
        """
        mw = self.get_selected_model_wrapper()
        return None if mw is None else mw.model

    def get_selected_component(self):
        """
        Returns the selected component
        """
        return self.tree.get_selected_component()

    def get_selected_plot(self):
        """
        Returns the selected signal; a string specifying whether the active
        window is "navtigation" plot, "signal" plot or "other"; and finally the
        active window.
        """
        s = self.get_selected_wrapper()
        w = self.main_frame.activeSubWindow()
        if w is s.navigator_plot:
            selected = "navigation"
        elif w is s.signal_plot:
            selected = "signal"
        else:
            selected = "other"
        return s, selected, w

    def select_signal(self, win, action):
        """Signal selection callback for actions that are only valid for
        selected Signals.
        """
        s = hyperspyui.util.win2sig(win, self.signals, self._plotting_signal)
        if s is None:
            action.setEnabled(False)
        else:
            action.setEnabled(True)

    def select_model(self, win, action):
        """Model selection callback for actions that are only valid for
        selected Models.
        """
        m = self.get_selected_model()
        action.setEnabled(m is not None)

    # --------- File I/O ----------

    @staticmethod
    def get_accepted_extensions():
        extensions = set([extensions.lower() for plugin in io_plugins
                          for extensions in plugin.file_extensions])
        return extensions

    def prompt_files(self, extension_filter=None):
        filenames = QFileDialog.getOpenFileNames(self,
                                                 tr('Load file'),
                                                 self.cur_dir,
                                                 extension_filter)
        # Pyside returns tuple, PyQt not
        if isinstance(filenames, tuple):
            filenames = filenames[0]
        return filenames

    def load_stack(self, filenames=None, stack_axis=None):
        if filenames is None:
            extensions = self.get_accepted_extensions()
            type_choices = ';;'.join(["*." + e for e in extensions])
            type_choices = ';;'.join(("Python code (*.py)", type_choices))
            type_choices = ';;'.join(("All types (*.*)", type_choices))
            filenames = self.prompt_files(type_choices)
            if not filenames:
                return False
            self.cur_dir = filenames[0]
        for i, f in enumerate(filenames):
            filenames[i] = glob_escape.sub(r'[\1]', f)    # glob escapes

        sig = hyperspy.io.load(filenames, stack=True, stack_axis=stack_axis)
        if isinstance(sig, list):
            for s in sig:
                s.plot()
        else:
            sig.plot()
        self.record_code('ui.load_stack({0}, {1})'.format(filenames,
                                                          stack_axis))

    def load(self, filenames=None):
        """
        Load 'filenames', or if 'filenames' is None, open a dialog to let the
        user interactively browse for files. It then load these files using
        hyperspy.io.load and wraps them and adds them to self.signals.
        """

        if filenames is None:
            extensions = self.get_accepted_extensions()
            type_choices = ';;'.join(["*." + e for e in extensions])
            type_choices = ';;'.join(("Python code (*.py)", type_choices))
            type_choices = ';;'.join(("All types (*.*)", type_choices))
            filenames = self.prompt_files(type_choices)
            if not filenames:
                return False
            self.cur_dir = filenames[0]

        files_loaded = []
        for filename in filenames:
            self.set_status("Loading \"" + filename + "\"...")
            ext = os.path.splitext(filename)[1]
            if ext == '.py':
                e = EditorWidget(self, self, filename)
                self.editors.append(e)
                e.show()
                continue
            self.setUpdatesEnabled(False)   # Prevent flickering during load
            try:
                escaped = glob_escape.sub(r'[\1]', filename)    # glob escapes
                sig = hyperspy.io.load(escaped)
                if isinstance(sig, list):
                    for s in sig:
                        s.plot()
                else:
                    sig.plot()
                files_loaded.append(filename)
            except (IOError, ValueError) as e:
                self.set_status("Failed to load \"" + filename + "\"")
                exc_type, exc_value, exc_traceback = sys.exc_info()
                tb = traceback.extract_tb(exc_traceback)[-1]
                warnings.warn_explicit(
                    ("Failed to load file '%s'. Internal exception:\n %s: %s"
                     % (filename, exc_type.__name__, str(e))),
                    RuntimeWarning, tb[0], tb[1])
            finally:
                self.setUpdatesEnabled(True)    # Always resume updates!

        if len(files_loaded) == 1:
            self.set_status("Loaded \"" + files_loaded[0] + "\"")
        elif len(files_loaded) > 1:
            self.set_status("Loaded %d files" % len(files_loaded))
        self.record_code('ui.load({0})'.format(files_loaded))

        return files_loaded

    def save(self, signals=None, filenames=None):
        logger.debug("entering save(), with args: %s, %s",
                     str(signals), str(filenames))
        if signals is None:
            signals = self.get_selected_wrappers()
            logger.debug("No signals passed, saving selection: %s",
                         str(signals))

        extensions = self.get_accepted_extensions()
        type_choices = ';;'.join(["*." + e for e in extensions])
        type_choices = ';;'.join(("All types (*.*)", type_choices))
        logger.debug("Save type choices: %s", type_choices)

        i = 0
        overwrite = None
        for s in signals:
            # Match signal to filename. If filenames has not been specified,
            # or there are no valid filename for curren signal index i, we
            # have to prompt the user.
            if filenames is None or len(
                    filenames) <= i or filenames[i] is None:
                path_suggestion = self.get_signal_filepath_suggestion(s)
                logger.debug("No filenames passed. Auto-suggestion: %s",
                             path_suggestion)
                filename = QFileDialog.getSaveFileName(self, tr("Save file"),
                                                       path_suggestion,
                                                       type_choices,
                                                       "All types (*.*)")[0]
                # Dialog should have prompted about overwrite
                overwrite = True
                if not filename:
                    logger.info("Not saving signal %s", str(s))
                    continue
            else:
                filename = filenames[i]
                overwrite = None    # We need to confirm overwrites
            i += 1
            s.signal.save(filename, overwrite)

    def get_signal_filepath_suggestion(self, signal, default_ext=None):
        if default_ext is None:
            default_ext = hyperspy.defaults_parser.preferences.General.\
                         default_file_format
        # Get initial suggestion for save dialog.  Use
        # original_filename metadata if present, or self.cur_dir if not
        if signal.signal.metadata.has_item('General.original_filename'):
            f = signal.signal.metadata.General.original_filename
        else:
            f = self.cur_dir

        # Analyze suggested filename
        base, tail = os.path.split(f)
        fn, ext = os.path.splitext(tail)

        # If no directory in filename, use self.cur_dir's dirname
        if base is None or base == "":
            base = os.path.dirname(self.cur_dir)
        # If extension is not valid, use the defualt
        extensions = self.get_accepted_extensions()
        if ext not in extensions:
            ext = default_ext
        # Filename itself is signal's name
        fn = signal.name
        if os.name == 'nt':
            fn = fn.replace("<", "[").replace(">", "]")
            fn = re.sub("[:\"|\?\*]", '', fn)
        # Build suggestion and return
        path_suggestion = os.path.sep.join((base, fn))
        path_suggestion = os.path.extsep.join((path_suggestion, ext))
        return path_suggestion

    # ---------- Drag and drop overloads ----------

    def dragEnterEvent(self, event):
        # Check file name extensions to see if we should accept
        extensions = set(self.get_accepted_extensions().union(('py',)))
        mimeData = event.mimeData()
        if mimeData.hasUrls():
            pathList = [url.toLocalFile() for url in mimeData.urls()]
            data_ext = set([os.path.splitext(p)[1][1:] for p in pathList])
            # Accept as long as we can read some of the files being dropped
            if 0 < len(data_ext.intersection(extensions)):
                event.acceptProposedAction()

#    def dragMoveEvent(event):
#        pass
#
#    def dragLeaveEvent(event):
#        pass

    def dropEvent(self, event):
        # Something has been dropped. Try to load all file urls
        mimeData = event.mimeData()
        if mimeData.hasUrls():
            pathList = [url.toLocalFile() for url in mimeData.urls()]
            if self.load(pathList):
                event.acceptProposedAction()

    # --------- Hyperspy progress bars ----------

    cancel_progressbar = Signal(int)

    def on_progressbar_wanted(self, pid, maxval, label):
        if pid in self.progressbars:
            progressbar = self.progressbars[pid]
            progressbar.setValue(0)
        else:
            progressbar = QProgressDialog(self)
            progressbar.setMinimumDuration(2000)
            progressbar.setMinimum(0)
        progressbar.setMaximum(maxval)
        progressbar.setWindowTitle("Processing")
        progressbar.setLabelText(label)

        if pid not in self.progressbars:
            def cancel():
                self.cancel_progressbar.emit(pid)

            progressbar.canceled.connect(cancel)
            progressbar.setWindowModality(Qt.WindowModal)

            self.progressbars[pid] = progressbar

    def on_progressbar_update(self, pid, value, txt=None):
        if pid not in self.progressbars:
            return
        self.progressbars[pid].setValue(value)
        if txt is not None:
            self.progressbars[pid].setLabelText(txt)

    def on_progressbar_finished(self, pid):
        progressbar = self.progressbars.pop(pid)
        progressbar.close()

    # --------- End hyperspy progress bars ----------

    # --------- Console functions ----------

    def on_console_executing(self, source):
        super(MainWindowHyperspy, self).on_console_executing(source)
#        self.setUpdatesEnabled(False)
        for s in self.signals:
            s.keep_on_close = True

    def on_console_executed(self, response):
        super(MainWindowHyperspy, self).on_console_executed(response)
        for s in self.signals:
            s.update_figures()
            s.keep_on_close = False
#        self.setUpdatesEnabled(True)

    def _get_console_exec(self):
        ex = super(MainWindowHyperspy, self)._get_console_exec()
        ex += '\nimport hyperspy.api as hs'
        ex += '\nimport numpy as np'
        return ex

    def _get_console_exports(self):
        push = super(MainWindowHyperspy, self)._get_console_exports()
        push['siglist'] = self.hspy_signals
        return push

    def _get_console_config(self):
        # ===== THIS ======
        try:
            from traitlets.config.loader import PyFileConfigLoader
        except ImportError:
            from IPython.config.loader import PyFileConfigLoader
        ipcp = os.path.sep.join((os.path.dirname(__file__), "ipython_profile",
                                 "ipython_embedded_config.py"))
        c = PyFileConfigLoader(ipcp).load_config()
        # ===== OR THIS =====
#        import hyperspy.Release
#        try:
#            from traitlets.config import Config
#        except ImportError:
#            from IPython.config import Config
#        c = Config()
#        c.FrontendWidget.banner = hyperspy.Release.info
        # ===== END =====
        return c