# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.


import imgui

from . import renderer
from .utils import EasyDict
from .gui_utils import imgui_utils

import slideflow as sf

#----------------------------------------------------------------------------

class ProjectWidget:
    def __init__(self, viz):
        self.viz            = viz
        self.search_dirs    = []
        self.cur_project      = None
        self.user_project     = ''
        self.recent_projects  = []
        self.browse_cache   = dict()
        self.browse_refocus = False
        self.cur_project    = None
        self.P              = None
        self.slide_paths    = []
        self.slide_idx      = 0

    def add_recent(self, project, ignore_errors=False):
        try:
            if project not in self.recent_projects:
                self.recent_projects.append(project)
        except:
            if not ignore_errors:
                raise

    def load(self, project, ignore_errors=False):
        viz = self.viz
        viz.clear_result()
        viz.skip_frame() # The input field will change on next frame.
        try:
            self.cur_project = project
            self.user_project = project

            viz.defer_rendering()
            if project in self.recent_projects:
                self.recent_projects.remove(project)
            self.recent_projects.insert(0, project)

            print("Loading project at {}...".format(project))
            self.P = sf.Project(project)
            self.slide_paths = self.P.dataset().slide_paths()
            viz.model_widget.search_dirs = [self.P.models_dir]
            viz.slide_widget.project_slides = self.slide_paths

        except Exception:
            self.cur_project = None
            self.user_project = project
            if project == '':
                viz.result = EasyDict(message='No project loaded')
            else:
                viz.result = EasyDict(error=renderer.CapturedException())
            if not ignore_errors:
                raise

    @imgui_utils.scoped_by_object_id
    def __call__(self, show=True):
        viz = self.viz
        recent_projects = [project for project in self.recent_projects if project != self.user_project]
        if show:
            bg_color = [0.16, 0.29, 0.48, 0.2]
            dim_color = list(imgui.get_style().colors[imgui.COLOR_TEXT])
            dim_color[-1] *= 0.5

            imgui.text('Project')
            imgui.same_line(viz.label_w)
            changed, self.user_project = imgui_utils.input_text('##project', self.user_project, 1024,
                flags=(imgui.INPUT_TEXT_AUTO_SELECT_ALL | imgui.INPUT_TEXT_ENTER_RETURNS_TRUE),
                width=(-1 - viz.button_w * 2 - viz.spacing * 2),
                help_text='<PATH>')
            if changed:
                self.load(self.user_project, ignore_errors=True)
            if imgui.is_item_hovered() and not imgui.is_item_active() and self.user_project != '':
                imgui.set_tooltip(self.user_project)
            imgui.same_line()
            if imgui_utils.button('Recent...', width=viz.button_w, enabled=(len(recent_projects) != 0)):
                imgui.open_popup('recent_projects_popup')
            imgui.same_line()
            if imgui_utils.button('Browse...', enabled=len(self.search_dirs) > 0, width=-1):
                imgui.open_popup('browse_projects_popup')
                self.browse_cache.clear()
                self.browse_refocus = True

        if imgui.begin_popup('recent_projects_popup'):
            for project in recent_projects:
                clicked, _state = imgui.menu_item(project)
                if clicked:
                    self.load(project, ignore_errors=True)
            imgui.end_popup()

        if imgui.begin_popup('browse_projects_popup'):
            def recurse(parents):
                key = tuple(parents)
                items = self.browse_cache.get(key, None)
                if items is None:
                    items = []
                    self.browse_cache[key] = items
                for item in items:
                    if item.type == 'run' and imgui.begin_menu(item.name):
                        recurse([item.path])
                        imgui.end_menu()
                    if item.type == 'project':
                        clicked, _state = imgui.menu_item(item.name)
                        if clicked:
                            self.load(item.path, ignore_errors=True)
                if len(items) == 0:
                    with imgui_utils.grayed_out():
                        imgui.menu_item('No results found')
            recurse(self.search_dirs)
            if self.browse_refocus:
                imgui.set_scroll_here()
                viz.skip_frame() # Focus will change on next frame.
                self.browse_refocus = False
            imgui.end_popup()

        paths = viz.pop_drag_and_drop_paths()
        if paths is not None and len(paths) >= 1:
            self.load(paths[0], ignore_errors=True)

#----------------------------------------------------------------------------
