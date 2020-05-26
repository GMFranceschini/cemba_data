"""
Demultiplex pipeline
"""

import locale
import logging
import pathlib
import re
import subprocess

import pandas as pd

import cemba_data
from .fastq_dataframe import make_fastq_dataframe
from .utilities import snakemake

# logger

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

PACKAGE_DIR = pathlib.Path(cemba_data.__path__[0])


def _demultiplex(fastq_pattern, output_dir, barcode_version, cpu):
    """
    Input raw FASTQ file pattern
    1. automatically parse the name to generate fastq dataframe
    2. use the FASTQ dataframe to generate dir structure for each uid
    3. generate snakefile for demultiplexing each uid
    4. generate final snakefile in output_dir
    5. execute snakefile

    Parameters
    ----------
    fastq_pattern
    output_dir
    barcode_version
    cpu

    Returns
    -------

    """
    output_dir = pathlib.Path(output_dir).absolute()

    # make fastq dataframe
    fastq_df = make_fastq_dataframe(fastq_pattern,
                                    barcode_version=barcode_version,
                                    output_path=output_dir / 'fastq_dataframe.csv')

    # prepare UID sub dir
    snakefile_list = []
    total_stats_list = []
    rule_count = 0
    for uid, uid_df in fastq_df.groupby('uid'):
        # determine index file path
        if barcode_version == 'V1':
            random_index_fasta_path = str(PACKAGE_DIR /
                                          'mapping/files/random_index_v1.fa')
        elif barcode_version == 'V2':
            multiplex_group = uid.split('-')[-2]
            random_index_fasta_path = str(
                PACKAGE_DIR / 'mapping/files/random_index_v2/'
                              f'random_index_v2.multiplex_group_{multiplex_group}.fa')
        else:
            raise ValueError(
                f'Unknown barcode version name: {barcode_version}.')

        # create a directory for each uid, within this UID, do multiplex and lane merge
        uid_output_dir = output_dir / uid
        uid_output_dir.mkdir(exist_ok=True)
        lane_files_dir = uid_output_dir / 'lanes'
        lane_files_dir.mkdir(exist_ok=True)

        # standardize input fastq name for easier parsing
        raw_dir = uid_output_dir / 'raw'
        raw_dir.mkdir(exist_ok=True)
        for _, row in uid_df.iterrows():
            uid, read_type, lane, old_path = row[[
                'uid', 'read_type', 'lane', 'fastq_path'
            ]]
            new_path = raw_dir / f'{uid}+{lane}+{read_type}.fq.gz'
            subprocess.run(['ln', '-s', old_path, new_path], check=True)
        lanes = list(uid_df['lane'].unique())
        name_str = '{{name}}'

        # make snakefile
        stats_out_list = [
            f'{lane_files_dir}/{uid}-{lane}.demultiplex.stats.txt'
            for lane in lanes
        ]
        total_stats_list += stats_out_list
        rules = ""
        for lane in lanes:
            snake_file_template = f"""
rule demultiplex_{rule_count}:
    input:
        r1_in = f'{raw_dir}/{uid}+{lane}+R1.fq.gz',
        r2_in = f'{raw_dir}/{uid}+{lane}+R2.fq.gz'
    params:
        r1_out = lambda wildcards: f'{lane_files_dir}/{uid}-{lane}-{name_str}-R1.fq.gz',
        r2_out = lambda wildcards: f'{lane_files_dir}/{uid}-{lane}-{name_str}-R2.fq.gz'
    output:
        stats_out = '{lane_files_dir}/{uid}-{lane}.demultiplex.stats.txt'
    shell:
        "cutadapt -Z -e 0.01 --no-indels -g file:{random_index_fasta_path} "
        "-o {{params.r1_out}} -p {{params.r2_out}} {{input.r1_in}} {{input.r2_in}} > {{output.stats_out}}"

    """
            rule_count += 1
            rules += snake_file_template

        snake_file_path = lane_files_dir / 'Snakefile'
        with open(snake_file_path, 'w') as f:
            f.write(rules)
        snakefile_list.append(f'{uid}/lanes/Snakefile')

    # make final snakefile for demultiplex step
    final_rules = ''
    for path in snakefile_list:
        final_rules += f'include: "{path}"\n'
    # final rules
    final_rules += f"""
rule final:
    input: {total_stats_list}
"""
    final_snake_path = output_dir / 'Snakefile_demultiplex'
    with open(final_snake_path, 'w') as f:
        f.write(final_rules)

    print('Demultiplexing raw FASTQ')
    snakemake(workdir=output_dir, snakefile=final_snake_path, cores=cpu)
    return


def _merge_lane(output_dir, cpu):
    output_dir = pathlib.Path(output_dir).absolute()
    fastq_df = pd.read_csv(output_dir / 'fastq_dataframe.csv')
    snakefile_list = []
    total_output_list = []
    rule_uid = 0
    # prepare snakefile in each uid
    for uid in fastq_df['uid'].unique():
        uid_output_dir = output_dir / uid
        lanes_dir = uid_output_dir / 'lanes'

        # prepare demultiplex results cell_fastq_df
        records = []
        for path in lanes_dir.glob('*fq.gz'):
            *uid, lane, index_name, read_type = path.name[:-6].split('-')
            uid = '-'.join(uid)
            cell_id = f'{uid}-{index_name}'
            records.append([cell_id, lane, read_type, str(path)])
        cell_fastq_df = pd.DataFrame(
            records, columns=['cell_id', 'index_name', 'read_type', 'fastq_path'])

        # prepare snakefile for each cell_id * read_type
        rules = ''
        output_paths = []
        for (cell_id, read_type), sub_df in cell_fastq_df.groupby(['cell_id', 'read_type']):
            input_paths = list(sub_df['fastq_path'])
            output_path = uid_output_dir / f'{cell_id}-{read_type}.fq.gz'

            snake_file_template = f"""
rule merge_{rule_uid}:
    input: 
        {input_paths}
    output: 
        "{output_path}"
    shell:
        "gzip -cd {{input}} | gzip -6 > {{output}} && rm -f {{input}}"

"""
            rule_uid += 1
            rules += snake_file_template
            output_paths.append(str(output_path))

        snakefile_path = uid_output_dir / 'Snakefile'
        with open(snakefile_path, 'w') as f:
            f.write(rules)
        snakefile_list.append(snakefile_path)
        total_output_list += output_paths

    # prepare final snakefile
    final_rules = ''
    for path in snakefile_list:
        final_rules += f'include: "{path}"\n'
    # final rules
    final_rules += f"""
rule final:
    input: {total_output_list}
"""
    final_snake_path = output_dir / 'Snakefile_merge_lane'
    with open(final_snake_path, 'w') as f:
        f.write(final_rules)

    print('Merging lanes to get cell FASTQ')
    subprocess.run(
        ['snakemake', '--snakefile',
         str(final_snake_path), '--cores',
         str(cpu)],
        check=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        encoding='utf8')
    return


def _parse_index_fasta(fasta_path):
    records = {}
    with open(fasta_path) as f:
        key_line = True
        for line in f:
            if key_line:
                key = line.lstrip('>').rstrip('\n')
                key_line = False
            else:
                value = line.lstrip('^').rstrip('\n')
                records[key] = value
                key_line = True
    return records


def _read_cutadapt_result(stat_path):
    """
    Parser of cutadapt output
    """
    with open(stat_path) as f:
        p = re.compile(
            r"Sequence: .+; Type: .+; Length: \d+; Trimmed: \d+ times")
        series = []
        total_pairs = -1
        for line in f:
            if line.startswith('Total read pairs processed'):
                # some weird transform of cutadapt outputs...
                locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
                total_pairs = locale.atoi(line.split(' ')[-1])

            m = p.search(line)
            if m is not None:
                result_dict = {}
                for i in m.group().split('; '):
                    k, v = i.split(': ')
                    result_dict[k] = v
                result_series = pd.Series(result_dict)
                series.append(result_series)
        total_df = pd.DataFrame(series)
        total_df['Trimmed'] = total_df['Trimmed'].apply(
            lambda c: c.split(' ')[0]).astype(int)
        total_df['TotalPair'] = total_pairs
        total_df['Ratio'] = total_df['Trimmed'] / total_pairs
    return total_df


def _summarize_demultiplex(output_dir, barcode_version):
    output_dir = pathlib.Path(output_dir).absolute()
    output_path = output_dir / 'demultiplex.stats.csv'
    barcode_version = barcode_version.upper()

    # get index info
    if barcode_version == 'V1':
        random_index_fasta_path = str(PACKAGE_DIR /
                                      'mapping/files/random_index_v1.fa')
    elif barcode_version == 'V2':
        # here we don't need to worry about the multiplex_group issue,
        # because we just need a index_name to index_seq map
        # we've considered this during demultiplex
        random_index_fasta_path = str(
            PACKAGE_DIR / 'mapping/files/random_index_v2/random_index_v2.fa')
    else:
        raise ValueError(
            f'Unknown version name {barcode_version} in multiplexIndex section of the config file.'
        )
    index_seq_dict = _parse_index_fasta(random_index_fasta_path)
    index_name_dict = {v: k for k, v in index_seq_dict.items()}

    # read the demultiplex stats, its per lane, so need to sum up lane together of each uid and index name
    # but R1 R2 is demultiplexed together, so this table don't separate R1 R2
    stat_list = []
    stat_path_list = list(output_dir.glob('*/lanes/*demultiplex.stats.txt'))
    for path in stat_path_list:
        single_df = _read_cutadapt_result(path)
        *uid, suffix = path.name.split('-')
        lane = suffix.split('.')[0]
        uid = '-'.join(uid)
        single_df['uid'] = uid
        single_df['lane'] = lane
        single_df['index_name'] = single_df['Sequence'].map(index_name_dict)
        assert single_df['index_name'].isna().sum() == 0
        stat_list.append(single_df)
    total_demultiplex_stats = pd.concat(stat_list)

    # calculate cell level table
    total_demultiplex_stats['cell_id'] = total_demultiplex_stats[
                                             'uid'] + '-' + total_demultiplex_stats['index_name']

    cell_table = total_demultiplex_stats.groupby('cell_id').agg({
        'Trimmed': 'sum',
        'TotalPair': 'sum',
        'index_name': lambda i: i.unique()[0],
        'uid': lambda i: i.unique()[0]
    })
    cell_table.rename(columns={
        'Trimmed': 'CellInputReadPairs',
        'TotalPair': 'MultiplexedTotalReadPairs',
        'index_name': 'IndexName',
        'uid': 'UID'
    },
        inplace=True)
    cell_table['CellBarcodeRatio'] = cell_table[
                                         'CellInputReadPairs'] / cell_table['MultiplexedTotalReadPairs']
    cell_table['BarcodeVersion'] = barcode_version
    cell_table.to_csv(output_path)
    return


def _final_cleaning(output_dir):
    """
    remove intermediate files
    """
    output_dir = pathlib.Path(output_dir)

    delete_patterns = [f'Snakefile_*', '*/lanes', '*/raw', '*/Snakefile', '*/*-unknown-R*.fq.gz']

    total_paths = []
    for pattern in delete_patterns:
        total_paths += list(map(str, output_dir.glob(pattern)))

    subprocess.run(['rm', '-rf'] + total_paths, check=True)
    return


def demultiplex_pipeline(fastq_pattern, output_dir, barcode_version, mode, cpu):
    output_dir = pathlib.Path(output_dir).absolute() / 'fastq'
    if output_dir.exists():
        print('Delete existing output_dir...')
        subprocess.run(['rm', '-rf', str(output_dir)], check=True)
        output_dir.mkdir()
    else:
        output_dir.mkdir(parents=True)

    barcode_version = barcode_version.upper()
    if barcode_version not in ['V1', 'V2']:
        raise ValueError(f'Barcode version can only be V1 or V2, got {barcode_version}')
    with open(output_dir / '.barcode_version', 'w') as f:
        f.write(barcode_version)

    mode = mode.lower()
    supported_tech = ['mc', 'mct', 'mc2t']
    if mode not in supported_tech:
        raise ValueError(f"Technologies should be in {supported_tech}, got {barcode_version}")
    with open(output_dir / '.mode', 'w') as f:
        f.write(mode)

    _demultiplex(
        fastq_pattern=fastq_pattern,
        output_dir=output_dir,
        barcode_version=barcode_version,
        cpu=cpu)
    _merge_lane(output_dir=output_dir, cpu=cpu)
    _summarize_demultiplex(output_dir=output_dir, barcode_version=barcode_version)
    _final_cleaning(output_dir=output_dir)
    return
