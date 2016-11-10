# -*- coding: utf-8 -*-
"""
Copyright (C) 2012-2016

This file is part of mason.

mason is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

mason is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with mason.  If not, see <http://www.gnu.org/licenses/>.
"""
from __future__ import division
from __future__ import print_function

import logging
import time
from collections import defaultdict
from functools import partial
from heapq import heappop, heappush
from itertools import chain
from math import ceil

from mason import butter
from mason.animation import AnimationEvent, AnimationFrame
from mason.compat import Rect, SDLRect
from mason.platform.graphics import RendererAB

logger = logging.getLogger(__file__)


class OrthographicTiler(RendererAB):
    """ Base for buffered tile renderers.
    """

    alpha_clear_color = 0, 0, 0, 0
    _always_clear = False  # force the buffer to be cleared when redrawing

    def __init__(self, data, size, clamp_camera=True, time_source=time.time):

        # default options
        self.data = data  # reference to data source
        self.clamp_camera = clamp_camera  # if true, cannot scroll past map edge
        self.anchored_view = True  # if true, map will be fixed to upper left corner
        self.map_rect = None  # rect of entire map in pixels
        self.time_source = time_source  # determines how tile animations are processed

        # private attributes
        self._clear_color = self.alpha_clear_color
        self._redraw_cutoff = None  # size of dirty tile edge that will trigger full redraw
        self._size = None  # size that the camera/viewport is on screen, kinda
        self._buffer = None  # complete rendering of tilemap
        self._buffer_size = None  # size of the buffer; typically larger than the screen
        self._tile_view = None  # this rect represents each tile on the buffer
        self._half_width = None  # 'half x' attributes are used to reduce division ops.
        self._half_height = None
        self._tile_queue = None  # tiles queued to be draw onto buffer
        self._last_time = None  # used for scheduling animations
        self._animation_queue = None  # priority queue of animation token;  schedules tile changes
        self._zoom_level = 1.0  # negative numbers make map smaller, positive: bigger
        self._sprite_offset = None

        # Hack for now
        if SDLRect is Rect:
            # pygame
            self._buffer_rect = Rect(0, 0, 0, 0)
        else:
            # pysdl2_cffi
            self._buffer_rect = SDLRect()

        # used to speed up animated tile redraws by keeping track of animated tiles
        # so they can be updated individually
        self._animation_flag = False
        self._animation_tiles = defaultdict(set)
        self._animation_map = dict()

        # this represents the viewable pixels, aka 'camera'
        self.view_rect = Rect(0, 0, 0, 0)

        self.reload_animations()
        self.set_size(size)

    def _change_offset(self, x, y):
        x, y = int(x), int(y)
        self._buffer_rect.x = -x
        self._buffer_rect.y = -y
        self._sprite_offset = x, y

    def _update_time(self):
        self._last_time = time.time() * 1000

    @staticmethod
    def _calculate_zoom_buffer_size(size, value):
        if value <= 0:
            print('zoom level cannot be zero or less')
            raise ValueError
        value = 1.0 / value
        return [int(round(i * value)) for i in size]

    def draw(self, sprites, surface=None, rect=None):
        """ Draw the map onto a surface
        """
        if self._animation_queue:
            self._process_animation_queue()
            if self._animation_flag:
                self.redraw_tiles()

        if not self.anchored_view:
            self._clear_screen()

        self._draw_map()
        self._copy_buffer()
        self._draw_surfaces(self._buffer, sprites)

    def _draw_surfaces(self, destination, surfaces):
        """ Draw surfaces onto buffer, then redraw tiles that cover them

        :param destination: destination
        :param surfaces: sequence of surfaces to blit
        """
        ox, oy = self._sprite_offset
        z_top = int(len(list(self.data.visible_tile_layers)))
        map_get = self._animation_map.get
        render_queue = list()
        render_queue_append = render_queue.append

        butterer = butter(self.data.tile_size, z_top, self._tile_view.topleft)

        for sprite in surfaces:
            # tokenize the sprite to blit
            sprite_surface, sprite_rect, sprite_layer = sprite
            token = sprite_layer, sprite_rect, sprite_surface, 0
            render_queue_append(token)

            # move the sprite rect to compensate for the buffer offset
            world_rect = sprite_rect.move(ox, oy)

            # get 3d area of the tiles that are interesting with the sprite
            damage = butterer(world_rect, sprite_layer)

            # mark the interesting tiles as damaged, which will cause them to be redrawn
            for z, position, tex_info, gid in self.damage_tiles(damage):
                tex_info = map_get(gid, tex_info)
                token = (z, position, tex_info, gid)
                render_queue.append(token)

        # sort tiles and surfaces for correct render order
        render_queue.sort()

        # copy tiles and sprites directly to the screen, not buffer
        copy_sprite = partial(self._copy_sprite, destination)
        for layer, position, surface, gid in render_queue:
            copy_sprite(surface, position)

    def damage_tiles(self, damage):
        """

        :param damage:
        :return:
        """
        # get all the covered tiles, in render order
        # tokenize each covered tile
        ox, oy = self._sprite_offset
        tw, th = self.data.tile_size
        left, top = self._tile_view.topleft

        for z, x1, y1, tex_info, gid in self.data.get_tile_images_by_cube(damage):
            # adjust for view
            x1 -= left
            y1 -= top

            # convert tile coords to screen coords
            token = z, (x1 * tw - ox, y1 * th - oy, tw, th), tex_info, gid

            yield token

    def damage_tiles2(self, damage):
        """

        :param damage:
        :return:
        """
        for z, x1, y1, tex_info, gid in self.data.get_tile_images_by_cube(damage):
            token = z, x1, y1, tex_info, gid
            yield token

    def redraw_tiles(self, destination=None):
        logger.warn('mason buffer redraw')
        if self._clear_color or self._always_clear:
            self._clear_buffer(self._buffer, self._clear_color)

        self._tile_queue = self.data.get_tile_images_by_rect(self._tile_view)
        self._flush_tile_queue(destination)

    def scroll(self, v):
        """ scroll the background in pixels

        :param v: (int, int)
        """
        self.center((v[0] + self.view_rect.centerx, v[1] + self.view_rect.centery))

    def center(self, coords):
        """ center the map on a pixel

        float numbers will be rounded.

        :param coords: (number, number)
        """
        x, y = [round(i, 0) for i in coords]
        self.view_rect.center = x, y

        mw, mh = self.data.map_size
        tw, th = self.data.tile_size

        self.anchored_view = ((self._tile_view.width < mw) or
                              (self._tile_view.height < mh))

        if self.anchored_view and self.clamp_camera:
            self.view_rect.clamp_ip(self.map_rect)

        x, y = self.view_rect.center

        if not self.anchored_view:
            # calculate offset and do not scroll the map layer
            # this is used to handle maps smaller than screen
            self._change_offset(x - self._half_width, y - self._half_height)

        else:
            # calc the new position in tiles and offset
            left, ox = divmod(x - self._half_width, tw)
            top, oy = divmod(y - self._half_height, th)
            self._change_offset(ox, oy)

            # get the difference on each axis by tile
            dx = int(left - self._tile_view.left)
            dy = int(top - self._tile_view.top)

            # adjust the view if the view has changed without a redraw
            if dx or dy:
                self._change_view(dx, dy)

    @property
    def zoom(self):
        """ Zoom the map in or out.

        Increase this number to make map appear to come closer to camera.
        Decrease this number to make map appear to move away from camera.

        Default value is 1.0
        This value cannot be negative or 0.0

        :return: float
        """
        return self._zoom_level

    @zoom.setter
    def zoom(self, value):
        buffer_size = self._calculate_zoom_buffer_size(self._size, value)
        self._zoom_level = value
        self._initialize_buffers(buffer_size)

    def set_size(self, size):
        """ Set the size of the map in pixels

        This is an expensive operation, do only when absolutely needed.

        :param size: (width, height) pixel size of camera/view of the group
        """
        buffer_size = self._calculate_zoom_buffer_size(size, self._zoom_level)
        self._size = [int(i) for i in size]
        self._initialize_buffers(buffer_size)

    def get_center_offset(self):
        """ Return x, y pair that will change world coords to screen coords
        :return: int, int
        """
        return (-self.view_rect.centerx + self._half_width,
                -self.view_rect.centery + self._half_height)

    def reload_animations(self):
        """ Reload animation information
        """
        self._update_time()
        self._animation_queue = list()

        for gid, frame_data in self.data.get_animations():
            frames = list()
            for frame_gid, frame_duration in frame_data:
                image = self.data.get_tile_image_by_gid(frame_gid)
                frames.append(AnimationFrame(image, frame_duration))

            ani = AnimationEvent(gid, frames)
            ani.next += self._last_time
            heappush(self._animation_queue, ani)

    def _queue_edge_tiles(self, dx, dy):
        """ Queue edge tiles and clear edge areas on buffer if needed

        :param dx: Edge along X axis to enqueue
        :param dy: Edge along Y axis to enqueue
        :return: None
        """
        # TODO: possibly clean animation tiles

        v = self._tile_view
        fill = partial(self._buffer.fill, self._clear_color)
        tw, th = self.data.tile_size
        self._tile_queue = iter([])

        def append(rect):
            self._tile_queue = chain(self._tile_queue, self.data.get_tile_images_by_rect(rect))
            if self._clear_color:
                fill(((rect[0] - v.left) * tw,
                      (rect[1] - v.top) * th,
                      rect[2] * tw, rect[3] * th))

        if dx > 0:  # right side
            append((v.right - 1, v.top, dx, v.height))

        elif dx < 0:  # left side
            append((v.left, v.top, -dx, v.height))

        if dy > 0:  # bottom side
            append((v.left, v.bottom - 1, v.width, dy))

        elif dy < 0:  # top side
            append((v.left, v.top, v.width, -dy))

    def _process_animation_queue(self):
        """

        :return:
        """
        self._update_time()
        self._tile_queue = list()
        self._animation_flag = False
        map_get = self._animation_map.get
        render_queue = list()
        z_top = int(len(list(self.data.visible_tile_layers)))

        # test if the next scheduled tile change is ready
        while self._animation_queue[0].next <= self._last_time:
            self._animation_flag = True

            # get the next tile change "animation token"
            token = heappop(self._animation_queue)

            # advance animation and reschedule it
            self._process_animation_token(token)

            # determine if this gid should be removed from the animation map
            # gids will be removed if the a tile is no longer on the screen
            needs_clear = False

            # iterate over all the tile positions on the screen for this gid
            for x, y in self._animation_tiles[token.gid]:

                # TODO: move to change_view and remove this check
                # determine if this tile is still on the buffer (checked by using the tile view)
                if not self._tile_view.collidepoint(x, y):
                    needs_clear = True
                    continue

                damage = x, y, 0, x, y, z_top
                for z, x1, y1, tex_info, gid in self.data.get_tile_images_by_cube(damage):
                    tex_info = map_get(gid, tex_info)
                    tile = z, x1, y1, tex_info, gid
                    render_queue.append(tile)

            # this will delete the set of tile locations that are checked for
            # animated tiles.  when the tile queue is flushed, any tiles in the
            # queue will be added again.  i choose to remove the set, rather
            # than removing the item in the set to reclaim memory over time...
            # though i could implement it by removing entries.  idk  -lt
            if needs_clear:
                del self._animation_tiles[token.gid]

        # sort tiles and surfaces for correct render order
        import operator

        getter = operator.itemgetter(0, 1, 2)
        render_queue.sort(key=getter)

        self._tile_queue = render_queue
        self._flush_tile_queue(self._buffer)

    def _process_animation_token(self, token):
        """

        :param token:
        :return:
        """
        # advance the animation frame index, looping by default
        if token.index == len(token.frames) - 1:
            token.index = 0
        else:
            token.index += 1

        # get the next frame to be displayed (index already changed)
        next_frame = token.frames[token.index]

        # reset the timer for the change
        token.next = next_frame.duration + self._last_time

        # set the animation map
        self._animation_map[token.gid] = next_frame.image

        # put token back into the queue
        heappush(self._animation_queue, token)

    def _initialize_buffers(self, view_size):
        """ Create the buffers to cache tile drawing

        :param view_size: (int, int): size of the draw area
        :return: None
        """
        tw, th = self.data.tile_size
        mw, mh = self.data.map_size
        buffer_tile_width = int(ceil(view_size[0] / tw) + 1)
        buffer_tile_height = int(ceil(view_size[1] / th) + 1)
        buffer_pixel_size = buffer_tile_width * tw, buffer_tile_height * th

        self.map_rect = Rect(0, 0, mw * tw, mh * th)
        self.view_rect.size = view_size
        self._tile_view = Rect(0, 0, buffer_tile_width, buffer_tile_height)
        self._redraw_cutoff = 1  # TODO: optimize this value
        self._create_buffers(view_size, buffer_pixel_size)
        self._half_width = view_size[0] // 2
        self._half_height = view_size[1] // 2
        self._x_offset = 0
        self._y_offset = 0

        # TODO: clear the animation map

        self.redraw_tiles(self._buffer)
