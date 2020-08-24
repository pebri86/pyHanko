import os
import struct
from hashlib import md5
from io import BytesIO

from pdf_utils import generic
from pdf_utils.generic import pdf_name, pdf_string
from pdf_utils.misc import peek

"""
Utility classes for writing PDF files.
Contains code from the PyPDF2 project, see LICENSE.PyPDF2
"""

VENDOR = 'pdfstamp'


def _derive_key(base_key, idnum, generation):
    # Ripped out of PyPDF2
    # See § 7.6.2 in ISO 32000
    pack1 = struct.pack("<i", idnum)[:3]
    pack2 = struct.pack("<i", generation)[:2]
    key = base_key + pack1 + pack2
    md5_hash = md5(key).digest()
    return md5_hash[:min(16, len(base_key) + 5)]


def _contiguous_xref_chunks(position_dict):
    """
    Helper method to divide the XRef table (or stream) into contiguous chunks.
    """
    previous_idnum = None
    current_chunk = []

    # iterate over keys in object ID order
    key_iter = sorted(position_dict.keys(), key=lambda t: t[1])
    (_, first_idnum), key_iter = peek(key_iter)
    for ix in key_iter:
        generation, idnum = ix

        # the idnum jumped, so yield the current chunk
        # and start a new one
        if current_chunk and idnum != previous_idnum + 1:
            yield first_idnum, current_chunk
            current_chunk = []
            first_idnum = idnum

        # append the object reference to the current chunk
        # (xref table requires position and generation entries)
        current_chunk.append((position_dict[ix], generation))
        previous_idnum = idnum

    # there is always at least one chunk, so this is fine
    yield first_idnum, current_chunk


def write_xref_table(stream, position_dict):
    xref_location = stream.tell()
    stream.write(b'xref\n')
    # Insert xref table subsections in contiguous chunks.
    # This is necessarily more complicated than the implementation
    # in PyPDF2 (see ISO 32000 § 7.5.4, esp. on updates), since
    # we need to handle incremental updates correctly.
    subsections = _contiguous_xref_chunks(position_dict)

    def write_header(idnum, length):
        header = '%d %d\n' % (idnum, length)
        stream.write(header.encode('ascii'))

    def write_subsection(chunk):
        for position, generation in chunk:
            entry = "%010d %05d n \n" % (position, generation)
            stream.write(entry.encode('ascii'))

    first_idnum, subsection = next(subsections)
    # TODO support deleting objects
    # case distinction: in contrast with the above we have to ensure that
    # everything is written in one chunk when *not* doing incremental updates.
    # In particular, this applies to the null object
    null_obj_ref = b'0000000000 65535 f \n'
    if first_idnum == 1:
        # integrate the null object into the first subsection
        write_header(0, len(subsection) + 1)
        stream.write(null_obj_ref)
        write_subsection(subsection)
    else:
        # insert origin of linked list of freed objects, and then the first
        # subsection, as usual
        stream.write(b'0 1\n')
        stream.write(null_obj_ref)
        write_header(first_idnum, len(subsection))
        write_subsection(subsection)
    for first_idnum, subsection in subsections:
        # subsection header: list first object ID + length of subsection
        write_header(first_idnum, len(subsection))
        write_subsection(subsection)

    return xref_location


class XRefStream(generic.StreamObject):

    def __init__(self, position_dict):
        super().__init__()
        self.position_dict = position_dict

        # type indicator is one byte wide
        # we use longs to indicate positions of objects (>Q)
        # two more bytes for the generation number of an uncompressed object
        widths = map(generic.NumberObject, (1, 8, 2))
        self.update({
            pdf_name('/W'): generic.ArrayObject(widths),
            pdf_name('/Type'): pdf_name('/XRef'),
        })

    def write_to_stream(self, stream, encryption_key):
        # the caller is responsible for making sure that the stream
        # is registered in the position dictionary
        if encryption_key is not None:
            raise ValueError('XRef streams cannot be encrypted')

        index = [0, 1]
        subsections = _contiguous_xref_chunks(self.position_dict)
        stream_content = BytesIO()
        # write null object
        stream_content.write(b'\x00' * 9 + b'\xff\xff')
        for first_idnum, subsection in subsections:
            index += [first_idnum, len(subsection)]
            for position, generation in subsection:
                # TODO support objects in object streams
                stream_content.write(b'\x01')
                stream_content.write(struct.pack('>Q', position))
                stream_content.write(struct.pack('>H', generation))
        index_entry = generic.ArrayObject(map(generic.NumberObject, index))

        self[pdf_name('/Index')] = index_entry
        self._data = stream_content.getbuffer()
        super().write_to_stream(stream, None)


resource_dict_names = map(pdf_name, [
    'ExtGState', 'ColorSpace', 'Pattern', 'Shading', 'XObject',
    'Font', 'ProcSet', 'Properties'
])


def init_xobject_dictionary(command_stream, box_width, box_height,
                            resources=None):
    resources = resources or generic.DictionaryObject()
    return generic.StreamObject({
        pdf_name('/BBox'): generic.ArrayObject(list(
            map(generic.FloatObject, (0.0, box_height, box_width, 0.0))
        )),
        pdf_name('/Resources'): resources,
        pdf_name('/Type'): pdf_name('/XObject'),
        pdf_name('/Subtype'): pdf_name('/Form')
    }, stream_data=command_stream)


class BasePdfFileWriter:
    output_version = (1, 7)

    def __init__(self, root, info, document_id, obj_id_start=0,
                 stream_xrefs=True):
        self.objects = {}
        self._lastobj_id = obj_id_start

        if isinstance(root, generic.IndirectObject):
            self._root = root
        else:
            self._root = self.add_object(root)

        if info is None or isinstance(info, generic.IndirectObject):
            self._info = info
        else:
            self._info = self.add_object(info)
        self._encrypt = self._encrypt_key = None
        self._document_id = document_id
        self.stream_xrefs = stream_xrefs

    def mark_update(self, obj_ref):
        pass

    @property
    def root(self):
        return self._root.get_object()

    def get_object(self, ido):
        if ido.pdf is not self:
            raise ValueError("pdf must be self")
        return self.objects[(ido.generation, ido.idnum)]

    def add_object(self, obj):
        self._lastobj_id += 1
        self.objects[(0, self._lastobj_id)] = obj
        return generic.IndirectObject(self._lastobj_id, 0, self)

    def _write_header(self, stream):
        pass

    def _write_objects(self, stream, object_position_dict):
        for ix in sorted(self.objects.keys()):
            generation, idnum = ix
            obj = self.objects[ix]
            object_position_dict[ix] = stream.tell()
            stream.write(('%d %d obj' % (idnum, generation)).encode('ascii'))
            if self._encrypt is not None and idnum != self._encrypt.idnum:
                key = _derive_key(self._encrypt_key, idnum, generation)
            else:
                key = None
            obj.write_to_stream(stream, key)
            stream.write(b'\nendobj\n')

    def _populate_trailer(self, trailer):
        # prepare trailer dictionary entries
        trailer[pdf_name('/Root')] = self._root
        if self._info is not None:
            trailer[pdf_name('/Info')] = self._info
        # before doing anything else, we attempt to load the crypto-relevant
        # data, so that we can bail early if something's not right
        trailer[pdf_name('/ID')] = self._document_id

    def write(self, stream):

        object_positions = {}

        if self.stream_xrefs:
            trailer = XRefStream(object_positions)
            trailer.compress()
        else:
            trailer = generic.DictionaryObject()

        self._write_header(stream)
        self._populate_trailer(trailer)
        self._write_objects(stream, object_positions)

        if self.stream_xrefs:
            xref_location = stream.tell()
            xrefs_id = self._lastobj_id + 1
            # add position of XRef stream to the XRef stream
            object_positions[(0, xrefs_id)] = xref_location
            trailer[pdf_name('/Size')] = generic.NumberObject(xrefs_id + 1)
            # write XRef stream
            stream.write(('%d %d obj' % (xrefs_id, 0)).encode('ascii'))
            trailer.write_to_stream(stream, None)
            stream.write(b'\nendobj\n')
        else:
            # classical xref table
            xref_location = write_xref_table(stream, object_positions)
            trailer[pdf_name('/Size')] = generic.NumberObject(
                self._lastobj_id + 1
            )
            # write trailer
            stream.write(b'trailer\n')
            trailer.write_to_stream(stream, None)

        # write xref table pointer and EOF
        xref_pointer_string = '\nstartxref\n%s\n' % xref_location
        stream.write(xref_pointer_string.encode('ascii') + b'%%EOF\n')

    def register_annotation(self, page_ref, annot_ref):
        page_obj = page_ref.get_object()
        try:
            annots_ref = page_obj.raw_get('/Annots')
            if isinstance(annots_ref, generic.IndirectObject):
                annots = annots_ref.get_object()
                self.mark_update(annot_ref)
            else:
                # we need to update the entire page object if the annots array
                # is a direct object
                annots = annots_ref
                self.mark_update(page_ref)
        except KeyError:
            annots = generic.ArrayObject()
            self.mark_update(page_ref)
            page_obj[pdf_name('/Annots')] = annots

        annots.append(annot_ref)

    # TODO write tests specifically for this helper function
    def _walk_page_tree(self, page_ix, retrieve_parent):

        # the spec says that this will always be an indirect reference
        page_tree_root_ref = self.root.raw_get('/Pages')
        assert isinstance(page_tree_root_ref, generic.IndirectObject)
        page_tree_root = page_tree_root_ref.get_object()
        try:
            root_resources = page_tree_root['/Resources']
        except KeyError:
            root_resources = generic.DictionaryObject()

        page_count = page_tree_root['/Count']
        if not (0 <= page_ix < page_count):
            raise ValueError('Page index out of range')

        def _recurse(first_page_ix, pages_obj_ref, last_rsrc_dict,
                     prev_last_indir):
            last_indir = prev_last_indir
            pages_obj = pages_obj_ref.get_object()
            kids = pages_obj.raw_get('/Kids')
            if isinstance(kids, generic.IndirectObject):
                last_indir = kids
                kids = kids.get_object()

            try:
                last_rsrc_dict = pages_obj.raw_get('/Resources')
            except KeyError:
                pass

            cur_page_ix = first_page_ix
            for kid_index, kid_ref in enumerate(kids):
                # If this is not the case, the child node cannot possibly have
                # a valid /Parent entry either, so let's assume that nobody
                # screws up their PDF generator THAT badly
                assert isinstance(kid_ref, generic.IndirectObject)

                kid = kid_ref.get_object()

                node_type = kid['/Type']
                if node_type == '/Pages':
                    # recurse into this branch if the page we need
                    # is part of it
                    desc_count = kid['/Count']
                    if cur_page_ix <= page_ix < cur_page_ix + desc_count:
                        return _recurse(
                            cur_page_ix, kid_ref, last_rsrc_dict,
                            kid_ref
                        )
                    cur_page_ix += desc_count
                elif node_type == '/Page':
                    if cur_page_ix == page_ix:
                        if retrieve_parent:
                            return (
                                pages_obj_ref, kid_index, last_rsrc_dict,
                                # we want to ignore the potential reference to
                                # /Kids in this case
                                prev_last_indir
                            )
                        else:
                            try:
                                last_rsrc_dict = kid.raw_get('/Resources')
                            except KeyError:
                                pass
                            return kid_ref, last_rsrc_dict
                    else:
                        cur_page_ix += 1
            # This means the PDF is not standards-compliant
            raise ValueError('Page not found')

        return _recurse(
            0, page_tree_root_ref, root_resources, page_tree_root_ref
        )

    def find_page_container(self, page_ix):
        """
        Retrieve the node in the page tree containing the
        page with index page_ix, along with the necessary objects to modify it
        in an incremental update scenario.

        :param page_ix:
            The (zero-indexed) number of the page for which we want to
            retrieve the parent.
        :return:
            A quadruple with the /Pages object (or a reference to it),
            the index of the target page in said /Pages object,
            (possibly inherited) resource dictionary, and a reference
            to the object that needs to be marked for an update
            if the /Pagees object is updated.
        """
        return self._walk_page_tree(page_ix, retrieve_parent=True)

    def find_page_for_modification(self, page_ix):
        """
        Retrieve the page with index page_ix from the page tree, along with
        the necessary objects to modify it in an incremental update scenario.

        :param page_ix:
            The (zero-indexed) number of the page to retrieve.
        :return:
            A with a reference to the page object and a
            (possibly inherited) resource dictionary.
        """
        return self._walk_page_tree(page_ix, retrieve_parent=False)

    def insert_page(self, new_page, after=None):
        """
        Insert a page object into the tree.

        :param new_page:
            Page object to insert.
        :param after:
            Page number (zero-indexed) after which to insert the page.
        :return:
            A reference to the newly inserted page.
        """
        if new_page['/Type'] != pdf_name('/Page'):
            raise ValueError('Not a page object')
        if '/Parent' in new_page:
            raise ValueError('/Parent must not be set.')

        page_tree_root_ref = self.root.raw_get('/Pages')
        if after is None:
            page_count = page_tree_root_ref.get_object()['/Count']
            after = page_count - 1

        if after == -1:
            # there are no pages yet, this will be the first
            pages_obj_ref = upd_boundary = page_tree_root_ref
            kid_ix = -1
        else:
            pages_obj_ref, kid_ix, _, upd_boundary \
                = self.find_page_container(after)

        pages_obj = pages_obj_ref.get_object()
        kids_ref = pages_obj.raw_get('/Kids')
        if isinstance(kids_ref, generic.IndirectObject):
            kids_update_boundary = kids_ref
            kids = kids_ref.get_object()
        else:
            kids = kids_ref
            kids_update_boundary = upd_boundary
        # increase page count for all parents
        parent = pages_obj
        while parent is not None:
            # can't use += 1 because of the way PyPDF2's generic types work
            count = parent['/Count']
            parent[pdf_name('/Count')] = generic.NumberObject(count + 1)
            parent = parent.get('/Parent')
        new_page_ref = self.add_object(new_page)
        kids.insert(kid_ix + 1, new_page_ref)
        new_page[pdf_name('/Parent')] = pages_obj_ref
        self.mark_update(upd_boundary)
        if upd_boundary != kids_update_boundary:
            self.mark_update(kids_update_boundary)

        return new_page_ref


class PageObject(generic.DictionaryObject):

    # TODO be more clever with inheritable required attributes,
    #  and enforce the requirements on insertion instead
    # (setting /MediaBox at the page tree root seems to make sense, for example)
    def __init__(self, contents, media_box, resources=None):
        resources = resources or generic.DictionaryObject()

        if isinstance(contents, list):
            if not all(map(generic.is_indirect, contents)):
                raise ValueError(
                    'Contents array must consist of indirect references'
                )
            if not isinstance(contents, generic.ArrayObject):
                contents = generic.ArrayObject(contents)
        elif not isinstance(contents, generic.IndirectObject):
            raise ValueError(
                'Contents must be either an indirect reference or an array'
            )

        if len(media_box) != 4:
            raise ValueError('Media box must consist of 4 coordinates.')
        super().__init__({
            pdf_name('/Type'): pdf_name('/Page'),
            pdf_name('/MediaBox'): generic.ArrayObject(
                map(generic.NumberObject, media_box)
            ),
            pdf_name('/Resources'): resources,
            pdf_name('/Contents'): contents
        })


class PdfFileWriter(BasePdfFileWriter):

    def __init__(self):
        # root object
        root = generic.DictionaryObject({
            pdf_name("/Type"): pdf_name("/Catalog"),
        })

        id1 = generic.ByteStringObject(os.urandom(16))
        id2 = generic.ByteStringObject(os.urandom(16))
        id_obj = generic.ArrayObject([id1, id2])

        # info object
        info = generic.DictionaryObject({
            pdf_name('/Producer'): pdf_string(VENDOR)
        })

        super().__init__(root, info, id_obj)

        pages = generic.DictionaryObject({
            pdf_name("/Type"): pdf_name("/Pages"),
            pdf_name("/Count"): generic.NumberObject(0),
            pdf_name("/Kids"): generic.ArrayObject(),
        })

        root[pdf_name('/Pages')] = self.add_object(pages)

    def _write_header(self, stream):
        major, minor = self.output_version
        stream.write(f'%PDF-{major}.{minor}\n'.encode('ascii'))
        # write some binary characters to make sure the file is flagged
        # as binary (see § 7.5.2 in ISO 32000-1)
        stream.write(b'%\xc2\xa5\xc2\xb1\xc3\xab\n')

    # I can't be arsed to actually implement encrypt() for newly written PDFs,
    # since all security handlers specified in the 1.7 standard are insecure as
    # hell. I'm going to keep the RC4 functionality in the incremental writer,
    # since we still want to be able to sign old PDF files.
