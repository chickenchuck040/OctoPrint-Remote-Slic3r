# coding=utf-8
from __future__ import absolute_import

__author__ = "Tim Hollabaugh"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"

import logging
import logging.handlers
import os
import flask
import re

import octoprint.plugin
import octoprint.util
import octoprint.slicing
import octoprint.settings

from .profile import Profile

import paramiko
from paramiko.client import SSHClient

blueprint = flask.Blueprint("plugin.slic3r", __name__)

class Slic3rPlugin(octoprint.plugin.SlicerPlugin,
                   octoprint.plugin.SettingsPlugin,
                   octoprint.plugin.TemplatePlugin,
                   octoprint.plugin.AssetPlugin,
                   octoprint.plugin.BlueprintPlugin,
                   octoprint.plugin.StartupPlugin):
	
	def __init__(self):
		self._slic3r_logger = logging.getLogger("octoprint.plugins.slic3r.engine")

		# setup job tracking across threads
		import threading
		self._slicing_commands = dict()
		self._slicing_commands_mutex = threading.Lock()
		self._cancelled_jobs = []
		self._cancelled_jobs_mutex = threading.Lock()

	##~~ StartupPlugin API

	def on_startup(self, host, port):
		# setup our custom logger
		slic3r_logging_handler = logging.handlers.RotatingFileHandler(self._settings.getPluginLogfilePath(postfix="engine"), maxBytes=2*1024*1024)
		slic3r_logging_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
		slic3r_logging_handler.setLevel(logging.DEBUG)

		self._slic3r_logger.addHandler(slic3r_logging_handler)
		self._slic3r_logger.setLevel(logging.DEBUG if self._settings.getBoolean(["debug_logging"]) else logging.CRITICAL)
		self._slic3r_logger.propagate = False
		
		self._logger.info("Starting ssh connection");
		
		ip = self._settings.get(["remote_ip"])
		user = self._settings.get(["remote_user"])
		
		global client
		global sftp

		client = SSHClient();
		client.load_system_host_keys()
		client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
		client.connect(ip.strip(), username=user.strip(), timeout=4)
		sftp = client.open_sftp()
	
	##~~ ShutdownPlugin API

	def on_shutdown(self):
		self._logger.info("Shutting Down")
		
	##~~ BlueprintPlugin API

	@octoprint.plugin.BlueprintPlugin.route("/import", methods=["POST"])
	def importSlic3rProfile(self):
		import datetime
		import tempfile

		input_name = "file"
		input_upload_name = input_name + "." + self._settings.globalGet(["server", "uploads", "nameSuffix"])
		input_upload_path = input_name + "." + self._settings.globalGet(["server", "uploads", "pathSuffix"])

		if input_upload_name in flask.request.values and input_upload_path in flask.request.values:
			filename = flask.request.values[input_upload_name]
			try:
				profile_dict, imported_name, imported_description = Profile.from_slic3r_ini(flask.request.values[input_upload_path])
				
			except Exception as e:
				return flask.make_response("Something went wrong while converting imported profile: {message}".format(e.message), 500)

		elif input_name in flask.request.files:
			temp_file = tempfile.NamedTemporaryFile("wb", delete=False)
			try:
				temp_file.close()
				upload = flask.request.files[input_name]
				upload.save(temp_file.name)
				profile_dict, imported_name, imported_description = Profile.from_slic3r_ini(temp_file.name)
			except Exception as e:
				return flask.make_response("Something went wrong while converting imported profile: {message}".format(e.message), 500)
			finally:
				os.remove(temp_file)

			filename = upload.filename

		else:
			return flask.make_response("No file included", 400)

		name, _ = os.path.splitext(filename)

		# default values for name, display name and description
		profile_name = _sanitize_name(name)
		profile_display_name = imported_name if imported_name is not None else name
		profile_description = imported_description if imported_description is not None else "Imported from {filename} on {date}".format(filename=filename, date=octoprint.util.getFormattedDateTime(datetime.datetime.now()))
		profile_allow_overwrite = False

		# overrides
		if "name" in flask.request.values:
			profile_name = flask.request.values["name"]
		if "displayName" in flask.request.values:
			profile_display_name = flask.request.values["displayName"]
		if "description" in flask.request.values:
			profile_description = flask.request.values["description"]
		if "allowOverwrite" in flask.request.values:
			from octoprint.server.api import valid_boolean_trues
			profile_allow_overwrite = flask.request.values["allowOverwrite"] in valid_boolean_trues

		self._slicing_manager.save_profile("remote-slic3r",
		                                   profile_name,
		                                   profile_dict,
		                                   allow_overwrite=profile_allow_overwrite,
		                                   display_name=profile_display_name,
		                                   description=profile_description)

		result = dict(
			resource=flask.url_for("api.slicingGetSlicerProfile", slicer="remote-slic3r", name=profile_name, _external=True),
			displayName=profile_display_name,
			description=profile_description
		)
		r = flask.make_response(flask.jsonify(result), 201)
		r.headers["Location"] = result["resource"]
		return r

	##~~ AssetPlugin API

	def get_assets(self):
		return {
			"js": ["js/slic3r.js"],
			"less": ["less/slic3r.less"],
			"css": ["css/slic3r.css"]
		}

	##~~ SettingsPlugin API

	def on_settings_save(self, data):
		old_debug_logging = self._settings.getBoolean(["debug_logging"])

		super(Slic3rPlugin, self).on_settings_save(data)

		new_debug_logging = self._settings.getBoolean(["debug_logging"])
		if old_debug_logging != new_debug_logging:
			if new_debug_logging:
				self._slic3r_logger.setLevel(logging.DEBUG)
			else:
				self._slic3r_logger.setLevel(logging.CRITICAL)
	
		global client
		global sftp
		
		client.close()
		
		ip = self._settings.get(["remote_ip"])
		user = self._settings.get(["remote_user"])
		
		client = SSHClient();
		client.load_system_host_keys()
		client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
		client.connect(ip.strip(), username=user.strip(), timeout=4)
		sftp = client.open_sftp()

	def get_settings_defaults(self):
		return dict(
			slic3r_engine=None,
			remote_ip=None,
			remote_user=None,
			remote_wdir="/tmp/",
			default_profile=None,
			debug_logging=False
		)

	##~~ SlicerPlugin API

	def is_slicer_configured(self):
		#slic3r = self._settings.get(["slic3r_engine"])
		#return slic3r is not None and os.path.exists(slic3r)
		return True;

	def get_slicer_properties(self):
		return dict(
			type="remote-slic3r",
			name="Remote Slic3r",
			same_device=False,
			progress_report=False
		)

	def get_slicer_default_profile(self):
		path = self._settings.get(["default_profile"])
		if not path:
			path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "profiles", "default.profile.ini")
		return self.get_slicer_profile(path)

	def get_slicer_profile(self, path):
		profile_dict, display_name, description = self._load_profile(path)

		properties = self.get_slicer_properties()
		return octoprint.slicing.SlicingProfile(properties["type"], "unknown", profile_dict, display_name=display_name, description=description)

	def save_slicer_profile(self, path, profile, allow_overwrite=True, overrides=None):
		from octoprint.util import dict_merge
		if overrides is not None:
			new_profile = dict_merge(profile.data, overrides)
		else:
			new_profile = profile.data

		self._save_profile(path, new_profile, allow_overwrite=allow_overwrite, display_name=profile.display_name, description=profile.description)

	def do_slice(self, model_path, printer_profile, machinecode_path=None, profile_path=None, position=None, on_progress=None, on_progress_args=None, on_progress_kwargs=None):
		if not profile_path:
			profile_path = self._settings.get(["default_profile"])
		if not machinecode_path:
			path, _ = os.path.splitext(model_path)
			machinecode_path = path + ".gco"

		self._logger.info("### Slicing %s to %s using profile stored at %s" % (model_path, machinecode_path, profile_path))
		
		self._logger.info(on_progress)
		self._logger.info(on_progress_args)
		self._logger.info(on_progress_kwargs)
		
		executable = self._settings.get(["slic3r_engine"])
		if not executable:
			return False, "Path to Slic3r is not configured "

		import sarge
		
		wdir = "/tmp/"
		
		remote_profile_path = wdir + os.path.basename(profile_path);
		remote_machinecode_path = wdir + os.path.basename(machinecode_path);
		remote_model_path = wdir + os.path.basename(model_path);

		working_dir, _ = os.path.split(executable)

		args = ['"%s"' % executable, '--load', '"%s"' % remote_profile_path, '-o', '"%s"' % remote_machinecode_path, '"%s"' % remote_model_path]

		command = " ".join(args)
		self._logger.info("Running %r in %s" % (command, working_dir))
		
		global client
		global sftp
		
		stdin, stdout, stderr = client.exec_command("uname -a")
		self._logger.info("Remote macine: %s" % (stdout.read(100)))
		
		self._logger.info("Transfering profile to %s on remote machine" % (remote_profile_path))
		sftp.put(profile_path, remote_profile_path)

		self._logger.info("Transfering model to %s on remote machine" % (remote_model_path))
		sftp.put(model_path, remote_model_path)
		
		self._logger.info("Slicing %s on remote machine" % (command))
		stdin, stdout, stderr = client.exec_command(command)
		self._logger.info("%s" % (stdout.read(1000)))
		
		self._logger.info("Transfering gcode to %s locally" % (machinecode_path))
		sftp.get(remote_machinecode_path, machinecode_path)
		
		
#		try:
#			p = sarge.run(command, cwd=working_dir, async=True, stdout=sarge.Capture(), stderr=sarge.Capture())
#			p.wait_events()
#			try:
#				with self._slicing_commands_mutex:
#					self._slicing_commands[machinecode_path] = p.commands[0]
#
#				line_seen = False
#				while p.returncode is None:
#					stdout_line = p.stdout.readline(timeout=0.5)
#					stderr_line = p.stderr.readline(timeout=0.5)
#
#					if not stdout_line and not stderr_line:
#						if line_seen:
#							break
#						else:
#							continue
#
#					line_seen = True
#					if stdout_line:
#						self._slic3r_logger.debug("stdout: " + stdout_line.strip())
#					if stderr_line:
#						self._slic3r_logger.debug("stderr: " + stderr_line.strip())
#			finally:
#				p.close()
#
#			with self._cancelled_jobs_mutex:
#				if machinecode_path in self._cancelled_jobs:
#					self._slic3r_logger.info("### Cancelled")
#					raise octoprint.slicing.SlicingCancelled()
#
#			self._slic3r_logger.info("### Finished, returncode %d" % p.returncode)
#			if p.returncode == 0:
#				return True, None
#			else:
#				self._logger.warn("Could not slice via Slic3r, got return code %r" % p.returncode)
#				return False, "Got returncode %r" % p.returncode
#
#		except octoprint.slicing.SlicingCancelled as e:
#			raise e
#		except:
#			self._logger.exception("Could not slice via Slic3r, got an unknown error")
#			return False, "Unknown error, please consult the log file"
#
#		finally:
#			with self._cancelled_jobs_mutex:
#				if machinecode_path in self._cancelled_jobs:
#					self._cancelled_jobs.remove(machinecode_path)
#			with self._slicing_commands_mutex:
#				if machinecode_path in self._slicing_commands:
#					del self._slicing_commands[machinecode_path]
#
#			self._slic3r_logger.info("-" * 40)
#
	def cancel_slicing(self, machinecode_path):
		with self._slicing_commands_mutex:
			if machinecode_path in self._slicing_commands:
				with self._cancelled_jobs_mutex:
					self._cancelled_jobs.append(machinecode_path)
				self._slicing_commands[machinecode_path].terminate()
				self._logger.info("Cancelled slicing of %s" % machinecode_path)

	def _load_profile(self, path):
		profile, display_name, description = Profile.from_slic3r_ini(path)
		return profile, display_name, description

	def _save_profile(self, path, profile, allow_overwrite=True, display_name=None, description=None):
		if not allow_overwrite and os.path.exists(path):
			raise IOError("Cannot overwrite {path}".format(path=path))
		Profile.to_slic3r_ini(profile, path, display_name=display_name, description=description)

	def _convert_to_engine(self, profile_path):
		profile = Profile(self._load_profile(profile_path))
		return profile.convert_to_engine()

def _sanitize_name(name):
	if name is None:
		return None

	if "/" in name or "\\" in name:
		raise ValueError("name must not contain / or \\")

	import string
	valid_chars = "-_.() {ascii}{digits}".format(ascii=string.ascii_letters, digits=string.digits)
	sanitized_name = ''.join(c for c in name if c in valid_chars)
	sanitized_name = sanitized_name.replace(" ", "_")
	return sanitized_name.lower()

__plugin_name__ = "Remote Slic3r"
__plugin_version__ = "0.1"
__plugin_implementations__ = [Slic3rPlugin()]
__plugin_description__ = "Slice files with Slic3r on a remote machine"
__plugin_author__ = "Tim Hollabaugh"
