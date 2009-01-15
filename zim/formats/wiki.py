# -*- coding: utf8 -*-

# Copyright 2008 Jaap Karssenberg <pardus@cpan.org>

'''FIXME'''

import re

from zim.fs import *
from zim.formats import *
from zim.utils import is_url_re, is_email_re, is_path_re, is_interwiki_re
from zim.utils import rfc822headers

info = {
	'name':  'Wiki text',
	'mime':  'text/x-zim-wiki',
	'read':	  True,
	'write':  True,
	'import': True,
	'export': True,
}

TABSTOP = 4
BULLET = u'[\\*\u2022]'

parser_re = {
	'blockstart': re.compile("\A(''')\s*?\n"),
	'pre':        re.compile("\A'''\s*?(^.*?)^'''\s*\Z", re.M | re.S),
	'splithead':  re.compile('^(==+[^\n\S]+\S.*?\n)', re.M),
	'heading':    re.compile("\A((==+)\s+(.*?)(\s+==+)?\s*)\Z"),
	'splitlist':  re.compile("((?:^\s*%s\s+.*\n?)+)" % BULLET, re.M),
	'listitem':   re.compile("^(\s*)(%s)\s+(.*\n?)" % BULLET),

	# All the experssions below will match the inner pair of
	# delimiters if there are more then two characters in a row.
	'link':   re.compile('\[\[(?!\[)(.+?)\]\]'),
	'img':    re.compile('\{\{(?!\{)(.+?)\}\}'),
	'em':     re.compile('//(?!/)(.+?)//'),
	'strong': re.compile('\*\*(?!\*)(.+?)\*\*'),
	'mark':   re.compile('__(?!_)(.+?)__'),
	'strike': re.compile('~~(?!~)(.+?)~~'),
	'code':   re.compile("''(?!')(.+?)''"),
}

dumper_tags = {
	'em':     '//',
	'strong': '**',
	'mark':   '__',
	'strike': '~~',
	'code':   "''",
}


class Parser(ParserClass):

	def parse(self, input):
		# Read the file and divide into paragraphs on the fly.
		# Blocks of empty lines are also treated as paragraphs for now.
		# We also check for blockquotes here and avoid splitting them up.
		assert isinstance(input, (File, Buffer))
		file = input.open('r')

		paras = ['']
		def para_start():
			# This function is called when we suspect the start of a new paragraph.
			# Returns boolean for success
			if len(paras[-1]) == 0:
				return False
			blockmatch = parser_re['blockstart'].search(paras[-1])
			if blockmatch:
				quote = blockmatch.group()
				blockend = re.search('\n'+quote+'\s*\Z', paras[-1])
				if not blockend:
					# We are in a block that is not closed yet
					return False
			# Else append empty paragraph to start new para
			paras.append('')
			return True

		para_isspace = False
		for line in file:
			# Try start new para when switching between text and empty lines or back
			if line.isspace() != para_isspace:
				if para_start():
					para_isspace = line.isspace() # decide type of new para
			paras[-1] += line
		file.close()

		# Now all text is read, start wrapping it into a document tree.
		# First check for meta data at the top of the file
		builder = TreeBuilder()
		if rfc822headers.match(paras[0]):
			headers = rfc822headers.parse(paras.pop(0))
			if paras[0].isspace: paras.pop(0)
			builder.start('page', headers)
		else:
			builder.start('page')

		# Then continue with all other contents
		# Headings can still be in the middle of a para, so get them out.
		for para in paras:
			if parser_re['blockstart'].search(para):
				self._parse_block(builder, para)
			else:
				parts = parser_re['splithead'].split(para)
				for i, p in enumerate(parts):
					if i % 2:
						# odd elements in the list are headings after split
						self._parse_head(builder, p)
					elif len(p) > 0:
						self._parse_para(builder, p)

		builder.end('page')
		return ParseTree(builder.close())

	def _parse_block(self, builder, block):
		'''Parse a block, like a verbatim paragraph'''
		m = parser_re['pre'].match(block)
		assert m, 'Block does not match pre'
		builder.start('pre')
		builder.data(m.group(1))
		builder.end('pre')

	def _parse_head(self, builder, head):
		'''Parse a heading'''
		m = parser_re['heading'].match(head)
		assert m, 'Line does not match a heading: %s' % head
		level = 7 - min(6, len(m.group(2)))
		builder.start('h', {'level': level})
		builder.data(m.group(3))
		builder.end('h')
		builder.data('\n')

	def _parse_para(self, builder, para):
		'''Parse a normal paragraph'''
		if para.isspace():
			builder.data(para)
		else:
			builder.start('p')
			parts = parser_re['splitlist'].split(para)
			for i, p in enumerate(parts):
				if i % 2:
					# odd elements in the list are lists after split
					self._parse_list(builder, p)
				elif len(p) > 0:
					self._parse_text(builder, p)
			builder.end('p')

	def _parse_list(self, builder, list):
		'''Parse a bullet list'''
		#~ m = parser_re['listitem'].match(list)
		#~ assert m, 'Line does not match a list item: %s' % line
		#~ prefix = m.group(1)
		#~ level = prefix.replace(' '*TABSTOP, '\t').count('\t')
		level = 0
		for i in range(-1, level):
			builder.start('ul')

		for line in list.splitlines():
			m = parser_re['listitem'].match(line)
			assert m, 'Line does not match a list item: %s' % line
			prefix, bullet, text = m.groups()

			mylevel = prefix.replace(' '*TABSTOP, '\t').count('\t')
			if mylevel > level:
				for i in range(level, mylevel):
					builder.start('ul')
			elif mylevel < level:
				for i in range(mylevel, level):
					builder.end('ul')
			level = mylevel

			builder.start('li')
			self._parse_text(builder, text)
			builder.end('li')

		for i in range(-1, level):
			builder.end('ul')

	def _parse_text(self, builder, text):
		'''Parse a piece of rich text, handles all inline formatting'''
		list = [text]
		list = self.walk_list(
				list, parser_re['code'],
				lambda match: ('code', {}, match) )

		def parse_link(match):
			parts = match.split('|', 2)
			link = parts[0]
			if len(parts) > 1:
				mytext = parts[1]
			else:
				mytext = link
			if len(link) == 0: # [[|link]] bug
					link = mytext
			if is_url_re.match(link): type = is_url_re[1]
			elif is_email_re.match(link): type = 'mailto'
			elif is_path_re.match(link): type = 'file'
			else: type = 'page'
			# TODO how about interwiki ?
			return ('link', {'type':type, 'href':link}, mytext)

		list = self.walk_list(list, parser_re['link'], parse_link)

		def parse_image(match):
			parts = match.split('|', 2)
			src = parts[0]
			if len(parts) > 1:
				mytext = parts[1]
			else:
				mytext = None
			return ('img', {'src':src}, mytext)

		list = self.walk_list(list, parser_re['img'], parse_image)

		for style in 'em', 'strong', 'mark', 'strike':
			list = self.walk_list(
					list, parser_re[style],
					lambda match: (style, {}, match) )

		# TODO: urls

		for part in list:
			if isinstance(part, tuple):
				builder.start(part[0], part[1])
				builder.data(part[2])
				builder.end(part[0])
			else:
				builder.data(part)


class Dumper(DumperClass):
	'''FIXME'''

	def dump(self, tree, output):
		'''FIXME'''
		assert isinstance(tree, ParseTree)
		assert isinstance(output, (File, Buffer))
		file = output.open('w')
		headers = rfc822headers.format(tree.getroot().attrib)
		file.write(headers)
		file.write('\n') # empty line to separate headers and data
		self.dump_children(tree.getroot(), file)
		file.close()

	def dump_children(self, list, file, list_level=-1):
		'''FIXME'''

		if list.text:
			file.write(list.text)

		for element in list.getchildren():
			if element.tag == 'p':
				self.dump_children(element, file) # recurs
			elif element.tag == 'ul':
				self.dump_children(element, file, list_level=list_level+1) # recurs
			elif element.tag == 'h':
				level = int(element.attrib['level'])
				if level < 1:   level = 1
				elif level > 5: level = 5
				tag = '='*(7 - level)
				file.write(tag+' '+element.text+' '+tag)
			elif element.tag == 'li':
				file.write('\t'*list_level+'* ')
				self.dump_children(element, file, list_level=list_level) # recurs
				file.write('\n')
			elif element.tag == 'pre':
				file.write("'''\n"+element.text+"'''\n")
			elif element.tag == 'img':
				src = element.attrib['src']
				if element.text:
					file.write('{{'+src+'|'+element.text+'}}')
				else:
					file.write('{{'+src+'}}')
			elif element.tag == 'link':
				href = element.attrib['href']
				if href == element.text:
					file.write('[['+href+']]')
				else:
					file.write('[['+href+'|'+element.text+']]')
			elif element.tag in dumper_tags:
				tag = dumper_tags[element.tag]
				file.write(tag+element.text+tag)
			else:
				assert False, 'Unknown node type: %s' % element

			if element.tail:
				file.write(element.tail)



