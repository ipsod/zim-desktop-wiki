# -*- coding: utf8 -*-

# Copyright 2009 Jaap Karssenberg <pardus@cpan.org>

'''This module contains an class that keeps an index of
all pages, links and backlinks in a notebook.
This index is stored as a sqlite database and allows efficient
lookups of the notebook structure.

To support the index acting as a cache the Store backends should support
a method "get_index_key(pagename)". This method should return a key that
changes when either the page or it's list of children changes (so changes to
the content of a child or the children of a child do not affect this key).
If this method is not implemented pages are re-indexed every time the index
is checked. If this method returns None the page and it's children do no
longer exist.

Note: there are some particular problems with storing hierarchical lists in
a asociative database. Especially lookups of page names are a bit inefficient,
as we need to do a seperate lookup for each parent. Open for future improvement.
'''

# Note that it is important that this module fires signals and list pages
# in a consistent order, if the order is not consistent or changes without
# the apropriate signals the pageindex widget will get confused and mess up.

import sqlite3
import gobject
import logging

from zim.notebook import Path, Link

logger = logging.getLogger('zim.index')

LINK_DIR_FORWARD = 1
LINK_DIR_BACKWARD = 2
LINK_DIR_BOTH = 3

# Primary keys start counting with "1", so we can use parent=0
# for pages in the root namespace...
# TODO: change this and have explicit entry for root...

# TODO: check if usage of indexkeys can be simplified to get rid of exception

# TODO: touch pages in the database that are linked but do not exist

SQL_CREATE_TABLES = '''
create table if not exists pages (
	id INTEGER PRIMARY KEY,
	basename TEXT,
	parent INTEGER DEFAULT '0',
	hascontent BOOLEAN,
	haschildren BOOLEAN,
	type INTEGER,
	ctime TIMESTAMP,
	mtime TIMESTAMP,
	contentkey TEXT,
	childrenkey TEXT
);
create table if not exists pagetypes (
	id INTEGER PRIMARY KEY,
	label TEXT
);
create table if not exists links (
	source INTEGER,
	href INTEGER,
	type INTEGER
);
create table if not exists linktypes (
	id INTEGER PRIMARY KEY,
	label TEXT
);
'''

# TODO need better support for TreePaths, e.g. as signal arguments for Treemodel

class IndexPath(Path):
	'''Like Path but adds more attributes, functions as an iterator for
	rows in the table with pages.'''

	__slots__ = ('_indexpath', '_row', '_pagelist_ref', '_pagelist_index')

	def __init__(self, name, indexpath, row=None):
		'''Constructore, needs at least a full path name and a tuple of index
		ids pointing to this path in the index. Row is an optional sqlite3.Row
		object and contains the actual data for this path. If row is given
		all properties can be queried as attributes of the IndexPath object.
		The property 'hasdata' is True when the IndexPath has row data.
		'''
		Path.__init__(self, name)
		self._indexpath = tuple(indexpath)
		self._row = row
		self._pagelist_ref = None
		self._pagelist_index = None
		# The pagelist attributes are not used in this module, but the
		# slot is reserved for usage in the PageTreeStore class to cache
		# a pagelist instead of doing the same query over and over again.

	@property
	def id(self): return self._indexpath[-1]

	@property
	def hasdata(self): return not self._row is None

	def __getattr__(self, attr):
		if self._row is None:
			raise AttributeError, 'This IndexPath does not contain row data'
		else:
			try:
				return self._row[attr]
			except IndexError:
				raise AttributeError, '%s has no attribute %s' % (self.__repr__, attr)

	def get_parent(self):
		'''Returns IndexPath for parent path'''
		namespace = self.namespace
		if namespace:
			return IndexPath(namespace, self._indexpath[:-1])
		elif self.isroot:
			return None
		else:
			return IndexPath(':', (0,))

	def parents(self):
		'''Generator function for parent namespace IndexPaths including root'''
		# version optimized to include indexpaths
		if ':' in self.name:
			path = self.name.split(':')
			path.pop()
			while len(path) > 0:
				namespace = ':'.join(path)
				indexpath = self._indexpath[:len(path)]
				yield IndexPath(namespace, indexpath)
				path.pop()
		yield IndexPath(':', (0,))


class Index(gobject.GObject):
	'''FIXME'''

	# define signals we want to use - (closure type, return type and arg types)
	__gsignals__ = {
		'page-inserted': (gobject.SIGNAL_RUN_LAST, None, (object,)),
		'page-updated': (gobject.SIGNAL_RUN_LAST, None, (object,)),
		'page-haschildren-toggled': (gobject.SIGNAL_RUN_LAST, None, (object,)),
		'page-deleted': (gobject.SIGNAL_RUN_LAST, None, (object,))
	}

	def __init__(self, notebook=None, dbfile=None):
		'''If no dbfile is given, the default file for this notebook will be
		used will be used. Main use of providing a dbfile here is to make the
		index operate in memory by setting dbfile to ":memory:".
		'''
		gobject.GObject.__init__(self)
		self.dbfile = dbfile
		self.db = None
		self.notebook = None
		self._updating= False
		self._update_pagelist_queue = []
		self._index_page_queue = []
		if self.dbfile:
			self._connect()
		if notebook:
			self.set_notebook(notebook)

	def set_notebook(self, notebook):
		self.notebook = notebook

		if not self.dbfile:
			# TODO index for RO notebooks
			#~ if notebook.isreadonly \
			#~ and not notebook.dir is None \
			#~ and notebook.dir.file('index.db').exists():
				#~ self.dbfile = notebook.dir.file('index.db')
			#~ else:
			if notebook.cache_dir is None:
				logger.debug('No cache dir found - loading index in memory')
				self.dbfile = ':memory:'
			else:
				notebook.cache_dir.touch()
				self.dbfile = notebook.cache_dir.file('index.db')
				logger.debug('Index database file: %s', self.dbfile)
			self._connect()

		# TODO connect to notebook signals for pages being moved / deleted /
		# modified

	def do_save_page(self, page):
		assert False, 'TODO: lookup page / touch if needed'
		self._index_page(page)

	def do_move_page(self, page):
		pass # TODO index logic for moving page(s)

	def do_delete_page(self, path):
		'''Delete page plus sub-pages plus forward links from the index'''
		# TODO actually delete a page + children + links
		# TODO emit signal for deleted pages
		path = self.lookup_path(path)
		if path.haschildren:
			self._delete_pagelist(path)
		self.db.commit()
		self.emit('page-deleted', path)

	def _connect(self):
		self.db = sqlite3.connect(
			str(self.dbfile), detect_types=sqlite3.PARSE_DECLTYPES)
		self.db.row_factory = sqlite3.Row

		# TODO verify database integrity and zim version number
		self.db.executescript(SQL_CREATE_TABLES)

	def flush(self):
		'''Flushes all database content. Can be used before calling
		update() to have a clean re-build. However, this method does not
		generate signals, so it is not safe to use while a PageTreeStore
		is connected to the index.
		'''
		logger.info('Flushing index')
		for table in ('pages', 'pagetypes', 'links', 'linktypes'):
			self.db.execute('drop table "%s"' % table)
		self.db.executescript(SQL_CREATE_TABLES)

	def update(self, path=None, background=False, checkcontents=True, callback=None):
		'''This method initiates a database update for a namespace, or,
		if no path is given for the root namespace of the notebook. For
		each path the indexkey as provided by the notebook store will be checked
		to decide if an update is needed. Note that if we have a new index which
		is still empty, updating will build the contents.

		If "background" is True the update will be scheduled on idle events
		in the glib / gtk main loop. Starting a second background job while
		one is already running just adds the new path in the queue.

		If "checkcontents" is True the indexkey for each page is checked to
		determine if the contents also need to be indexed. If this option
		is False only pagelists will be updated. Any new pages that are
		encoutered are always indexed fully regardless of this option.

		A callback method can be supplied that will be called after each
		updated path. This can be used e.g. to display a progress bar. the
		callback gets the path just processed as an argument. If the callback
		returns False the update will not continue.

		Indexes are checked width first. This is important to make the visual
		behavior of treeviews displaying the index look more solid.
		'''

		# Updating uses two queues, one for indexing the tree structure and a
		# second for pages where we need to index the content. Reason is that we
		# first need to have the full tree before we can reliably resolve links
		# and thus index content.

		if path is None or path.isroot:
			indexpath = IndexPath(':', (0,), {'hascontent': False, 'haschildren':True})
		else:
			indexpath = self.lookup_path(path)
			if indexpath is None:
				p = self._touch_path(path)
				indexpath = IndexPath(path.name, p._indexpath, {'haschildren': True})
				checkcontent = True

		self._update_pagelist_queue.append(indexpath)
		if checkcontents and not indexpath.isroot:
			self._index_page_queue.append(indexpath)

		if background:
			if not self._updating:
				logger.info('Starting background index update')
				self._updating = False
				gobject.idle_add(self._do_update, (checkcontents, callback))
		else:
			logger.info('Updating index')
			while self._do_update((checkcontents, callback)):
				continue

	def _do_update(self, data):
		# This returns boolean to continue or not because it can be called as an
		# idle event handler, if a callback is used, the callback should give
		# this boolean value.
		checkcontents, callback = data
		if self._update_pagelist_queue or self._index_page_queue:
			try:
				if self._update_pagelist_queue:
					path = self._update_pagelist_queue.pop(0)
					self._update_pagelist(path, checkcontents)
				elif self._index_page_queue:
					path = self._index_page_queue.pop(0)
					page = self.notebook.get_page(path)
					self._index_page(path, page)
			except KeyboardInterrupt:
				raise
			except:
				# Catch any errors while listing & parsing all pages
				import sys
				logger.warn('Got an exception while indexing: %s', path)
				sys.excepthook(*sys.exc_info())

			if not callback is None:
				cont = callback(path)
				if not cont is True:
					logger.info('Index update is cancelled')
					self._update_pagelist_queue = [] # flush
					self._index_page_queue = [] # flush
					return False
			return True
		else:
			logger.info('Index update done')
			self._updating = False
			return False

	def _touch_path(self, path):
		'''This method creates a path along with all it's parents.
		Returns the final IndexPath.
		'''
		try:
			cursor = self.db.cursor()
			names = path.parts
			parentid = 0
			indexpath = []
			inserted = []
			for i in range(len(names)):
				p = self.lookup_path(Path(names[:i+1]))
				if p is None:
					haschildren = i < (len(names) - 1)
					cursor.execute(
						'insert into pages(basename, parent, hascontent, haschildren) values (?, ?, ?, ?)',
						(names[i], parentid, False, haschildren))
					parentid = cursor.lastrowid
					indexpath.append(parentid)
					inserted.append(
						IndexPath(':'.join(names[:i+1]), indexpath,
							{'hascontent': False, 'haschildren': haschildren}))
				else:
					# TODO check if haschildren is correct, update and emit has-children-toggled if not
					parentid = p.id
					indexpath.append(parentid)

			self.db.commit()
		except:
			self.db.rollback()
			raise
		else:
			for path in inserted:
				self.emit('page-inserted', path)

		return inserted[-1]

	def _index_page(self, path, page):
		'''Indexes page contents for page.

		TODO: emit a signal for this for plugins to use
		'''
		#~ print 'INDEX PAGE', path
		assert isinstance(path, IndexPath) and not path.isroot
		try:
			self.db.execute('delete from links where source == ?', (path.id,))
			for link in page.get_links():
				# TODO ignore links that are not internal
				href = self.notebook.resolve_path(
					link.href, namespace=page.get_parent(), index=self)
					# need to specify index=self here because we are
					# not necessary the default index for the notebook
				if not href is None:
					href = self.lookup_path(href)
				if not href is None:
					# TODO lookup href type
					self.db.execute('insert into links (source, href) values (?, ?)', (path.id, href.id))
			try:
				key = self.notebook.get_page_indexkey(page)
			except NotImplementedError:
				key = None
			self.db.execute('update pages set contentkey = ? where id == ?', (key, path.id))
			self.db.commit()
		except:
			self.db.rollback()
			raise
		else:
			self.emit('page-updated', path)

	def _update_pagelist(self, path, checkcontent):
		'''Checks and updates the pagelist for a path if needed and queues any
		child pages for updating based on "checkcontents" and whether
		the child has children itself. Called indirectly by update().
		'''
		#~ print 'UPDATE LIST', path
		assert isinstance(path, IndexPath)

		def check_and_queue(path):
			# Helper function to queue individual children
			if path.haschildren:
				self._update_pagelist_queue.append(path)
			elif checkcontent:
				uptodate = False
				try:
					pagekey = self.notebook.get_page_indexkey(path)
					if pagekey and path.contentkey == pagekey:
						uptodate = True
				except NotImplementedError:
					pass # we don't know

				if not uptodate:
					self._index_page_queue.append(path)

		# Check if listing is uptodate
		uptodate = False
		try:
			listkey = self.notebook.get_pagelist_indexkey(path)
			if listkey and path.childrenkey == listkey:
				uptodate = True
		except (KeyError, NotImplementedError):
			# TODO: we catch the KeyError here in case we get a path, e.g. roto,
			#which has a manually contructed row, which does not contain 'childrenkey'
			listkey = None # we don't know

		cursor = self.db.cursor()
		cursor.execute('select * from pages where parent==?', (path.id,))

		if uptodate:
			for row in cursor:
				p = IndexPath(path.name+':'+row['basename'], path._indexpath+(row['id'],), row)
				check_and_queue(p)
		else:
			children = {}
			for row in cursor:
				children[row['basename']] = row
			seen = set()
			changes = []
			try:
				for page in self.notebook.get_pagelist(path):
					seen.add(page.basename)
					if page.basename in children:
						# TODO: check if hascontent and haschildren are correct, update if incorrect + append to changes
						row = children[page.basename]
						p = IndexPath(path.name+':'+row['basename'], path._indexpath+(row['id'],), row)
						check_and_queue(p)
					else:
						# We set haschildren to False untill we have actualy seen those
						# children. Failing to do so will cause trouble with the
						# gtk.TreeModel interface to the database, which can not handle
						# nodes that say they have children but fail to deliver when
						# asked.
						cursor = self.db.cursor()
						cursor.execute(
							'insert into pages(basename, parent, hascontent, haschildren) values (?, ?, ?, ?)',
							(page.basename, path.id, page.hascontent, False))
						indexpath = path._indexpath + (cursor.lastrowid,)
						child = IndexPath(page.name, indexpath,
							{'hascontent': page.hascontent, 'haschildren': page.haschildren})
						changes.append((child, 1))
						if page.haschildren:
							self._update_pagelist_queue.append(child)
						if page.hascontent:
							self._index_page_queue.append(child)

				# Update index key to reflect we did our updates
				self.db.execute(
					'update pages set childrenkey = ? where id == ?',
					(listkey, path.id) )

				if path.isroot:
					self.db.commit()
				else:
					haschildren = len(seen) > 0
					self.db.execute(
						'update pages set haschildren=? where id==?',
						(haschildren, path.id) )
					self.db.commit()
			except:
				self.db.rollback()
				raise
			else:
				if not path.isroot:
					# FIXME should we check if this really changed first ?
					self.emit('page-haschildren-toggled', path)

				#~ for basename in set(children.keys()).difference(seen):
					#~ child = IndexPath(...)
					#~ changes.append((child, 3))

				# All these signals should come in proper order...
				changes.sort(key=lambda c: c[0].basename)
				for path, action in changes:
					if action == 1:
						self.emit('page-inserted', path)
					elif action == 2:
						self.emit('page-updated', path)
					else: # 3
						pass #~ self.do_delete_page(path)


	def walk(self, path=None):
		if path is None or path.isroot:
			return self._walk(IndexPath(':', (0,)), ())
		else:
			path = self.lookup_path(path)
			if path is None:
				raise ValueError
			return self._walk(path, path._indexpath)

	def _walk(self, path, indexpath):
		# Here path always is an IndexPath
		cursor = self.db.cursor()
		cursor.execute('select * from pages where parent == ? order by basename', (path.id,))
		for row in cursor:
			name = path.name+':'+row['basename']
			childpath = indexpath+(row['id'],)
			child = IndexPath(name, childpath, row)
			yield child
			if child.haschildren:
				for grandchild in self._walk(child, childpath):
					yield grandchild

	def lookup_path(self, path, parent=None):
		'''Returns an IndexPath for path. This method is mostly intended
		for internal use only, but can be used by other modules in
		some cases to optimize repeated index lookups. If a parent IndexPath
		is known this can be given to speed up the lookup.
		'''
		# Constructs the indexpath downward
		if isinstance(path, IndexPath):
			return path

		indexpath = []
		if parent and not parent.isroot:
			indexpath.extend(parent._indexpath)
		elif hasattr(path, '_indexpath'):
			# Page objects copy the _indexpath attribute
			indexpath.extend(path._indexpath)

		names = path.name.split(':')
		if indexpath:
			names = names[len(indexpath):] # shift X items
			parentid = indexpath[-1]
		else:
			parentid = 0

		cursor = self.db.cursor()
		if not names: # len(indexpath) was len(names)
			cursor.execute('select * from pages where id==?', (indexpath[-1],))
			row = cursor.fetchone()
		else:
			for name in names:
				cursor.execute(
					'select * from pages where basename==? and parent==?',
					(name, parentid) )
				row = cursor.fetchone()
				if row is None:
					return None # path is not indexed
				indexpath.append(row['id'])
				parentid = row['id']

		return IndexPath(path.name, indexpath, row)

	def lookup_data(self, path):
		'''Returns a full IndexPath for a IndexPath that has 'hasdata'
		set to False.
		'''
		cursor = self.db.cursor()
		cursor.execute('select * from pages where id==?', (path.id,))
		path._row = cursor.fetchone()
		return path

	def lookup_id(self, id):
		'''Returns an IndexPath for an index id'''
		# Constructs the indexpath upwards
		cursor = self.db.cursor()
		cursor.execute('select * from pages where id==?', (id,))
		row = cursor.fetchone()
		if row is None:
			return None # no such id !?

		indexpath = [row['id']]
		names = [row['basename']]
		parent = row['parent']
		while parent != 0:
			indexpath.insert(0, parent)
			cursor.execute('select basename, parent from pages where id==?', (parent,))
			row = cursor.fetchone()
			names.insert(0, row['basename'])
			parent = row['parent']

		return IndexPath(':'.join(names), indexpath, row)

	def resolve_case(self, name, namespace=None):
		'''Construct an IndexPath or Path by doing a case insensitive lookups
		for pages matching these name. If the full sub-page is found an
		IndexPath is returned. If at least the first part of the name is found
		an a Path is returned with the part that was found in the correct case
		and the remaining parts in the original case. If no match is found at
		all None is returned. If a parent namespace is given, the page name is
		resolved as a (indirect) sub-page of that path while assuming the case
		of the parent path is correct.
		'''
		if namespace and not namespace.isroot:
			parent = self.lookup_path(namespace)
			if parent is None:
				return None # parent does not even exist
			else:
				parentid = parent.id
				indexpath = list(parent._indexpath)
		else:
			parent = Path(':')
			parentid = 0
			indexpath = []

		names = name.split(':')
		found = []
		cursor = self.db.cursor()
		for name in names:
			cursor.execute(
				'select * from pages where lower(basename)==lower(?) and parent==?',
				(name, parentid) )
			rows = {}
			for row in cursor.fetchall():
				rows[row['basename']] = row

			if not rows:
				# path is not indexed
				if found: # but at least we found some match
					found.extend(names[len(found):]) # pad remaining names
					if not parent.isroot: found.insert(0, parent.name)
					return Path(':'.join(found))
					# FIXME should we include an indexpath here ?
				else:
					return None
			elif name in rows: # exact match
				row = rows[name]
			else: # take first insensitive match based on sorting
				n = rows.keys()
				n.sort()
				row = rows[n[0]]

			indexpath.append(row['id'])
			parentid = row['id']
			found.append(row['basename'])

		if not parent.isroot: found.insert(0, parent.name)
		return IndexPath(':'.join(found), indexpath, row)

	def list_pages(self, path):
		'''Returns a list of IndexPath objects for the sub-pages of 'path', or,
		if no path is given for the root namespace of the notebook.
		'''
		if path is None or path.isroot:
			parentid = 0
			name = ''
			indexpath = ()
		else:
			path = self.lookup_path(path)
			if path is None:
				return []
			parentid = path.id
			name = path.name
			indexpath = path._indexpath

		cursor = self.db.cursor()
		cursor.execute('select * from pages where parent==? order by basename', (parentid,))
		return [
			IndexPath(name+':'+r['basename'], indexpath+(r['id'],), r)
				for r in cursor ]

	def list_links(self, path, direction=LINK_DIR_FORWARD):
		path = self.lookup_path(path)
		if path:
			cursor = self.db.cursor()
			if direction == LINK_DIR_FORWARD:
				cursor.execute('select * from links where source == ?', (path.id,))
			elif direction == LINK_DIR_BOTH:
				cursor.execute('select * from links where source == ? or href == ?', (path.id, path.id))
			else:
				cursor.execute('select * from links where href == ?', (path.id,))

			for link in cursor:
				if link['source'] == path.id:
					source = path
					href = self.lookup_id(link['href'])
				else:
					source = self.lookup_id(link['source'])
					href = path
				# TODO lookup type by id

				yield Link(source, href)

	def n_list_links(self, path, direction=LINK_DIR_FORWARD):
		'''Link list_lins() but returns only the number of links instead
		of the links themselves.
		'''
		# TODO optimize this one
		return len(list(self.list_links(path, direction)))

	def get_previous(self, path):
		'''Returns the next page in the index, crossing namespaces'''
		# TODO get_previous

	def get_next(self, path):
		'''Returns the next page in the index, crossing namespaces'''
		# TODO get_next


# Need to register classes defining gobject signals
gobject.type_register(Index)