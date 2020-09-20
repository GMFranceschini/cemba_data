import pandas as pd


def _parse_cell_id_v1(cell_id):
    plate1, plate2, pcr_index, random_index = cell_id.split('-')
    if random_index.upper() in {'AD001', 'AD002', 'AD004', 'AD006'}:
        plate = plate1
    else:
        plate = plate2
    # 96 pos
    col96 = int(pcr_index[1:]) - 1
    row96 = ord(pcr_index[0]) - 65  # convert A-H to 0-8
    # 384 pos
    ad_index_384_dict = {
        'AD001': (0, 0),
        'AD002': (0, 1),
        'AD004': (1, 0),
        'AD006': (1, 1),
        'AD007': (0, 0),
        'AD008': (0, 1),
        'AD010': (1, 0),
        'AD012': (1, 1)
    }
    col384 = 2 * col96 + ad_index_384_dict[random_index][0]
    row384 = 2 * row96 + ad_index_384_dict[random_index][1]
    record = pd.Series({
        'Plate': plate,
        'PCRIndex': pcr_index,
        'RandomIndex': random_index,
        'Col384': col384,
        'Row384': row384
    })
    return record


def _parse_cell_id_v2(cell_id):
    plate, multiplex_group, pcr_index, random_index = cell_id.split('-')
    # 384 pos
    col384 = int(random_index[1:]) - 1
    row384 = ord(random_index[0]) - 65  # convert A-P to 0-23
    record = pd.Series({
        'Plate': plate,
        'PCRIndex': pcr_index,
        'MultiplexGroup': multiplex_group,
        'RandomIndex': random_index,
        'Col384': col384,
        'Row384': row384
    })
    return record


def get_plate_info(cell_ids, barcode_version):
    if barcode_version == 'V1':
        func = _parse_cell_id_v1
    else:
        func = _parse_cell_id_v2
    plate_info = pd.DataFrame([func(cell_id) for cell_id in cell_ids],
                              index=cell_ids)
    return plate_info
