
from collections import defaultdict
import gzip
import logging
import math
import multiprocessing 
import numpy as np
from optparse import OptionParser, SUPPRESS_HELP
import os
import pickle
import random
import sys
from subprocess import call
import time
import pandas as pd
import pybedtools

import clipper
from clipper import data_dir
from clipper.src.call_peak import call_peaks, poissonP
from contextlib import closing

logging.captureWarnings(True)
#logging.basicConfig(level=logging.INFO)


def check_for_index(bamfile):
    
    """

    Checks to make sure a BAM file has an index, if the index does not exist it is created
    
    Usage undefined if file does not exist (check is made earlier in program)
    bamfile - a path to a bam file
    
    """

    if not os.path.exists(bamfile):
        raise NameError("file %s does not exist" % (bamfile))
    
    if os.path.exists(bamfile + ".bai"):
        return 
    if not bamfile.endswith(".bam"):
        raise NameError("file %s not of correct type" % (bamfile))
    else:
        logging.info("Index for %s does not exist, indexing bamfile" % (bamfile))
        
        process = call(["samtools", "index", str(bamfile)])
        
        if process == -11: 
            raise NameError("file %s not of correct type" % (bamfile))
        
def build_geneinfo(bed):
    
    """

    Loads bed file into a dictionary with the key being the name and a string being the value
    
    Input:
    BED -- a bed file to load
    
    Return:
    A dictionary with the key being the name position of the bed file and the values being the
    ordered bed file
    
    """
    
    #opens bed file, either zipped or unzipped
    try:
        bedfile = gzip.open(bed, "rb")
    except IOError:
        bedfile = open(bed, "r")
        
    gene_info = dict()
    
    for line in bedfile.readlines():
        chromosome, start, stop, name, score, signstrand = line.strip().split()
        gene_info[name] = [chromosome, name, int(start), 
                           int(stop), str(signstrand)]
    
    bedfile.close()
    return gene_info

def build_lengths(length_file):
    
    """
    
    Builds a dictionary of gene names and lengths of mappable regions in that gene
    
    Input:
    A two column file with the first column being the gene name and the second column being the
    mappable length of the gene
    
    Return:
    A dictionary with the key being the name of the gene and the value being the length
    
    """
    
    try:
        handle = open(length_file, "r")
        gene_lengths = {}
    
        for line in handle.readlines():
            name, gene_length = line.strip().split("\t")
            gene_lengths[name] = int(gene_length)
    
        handle.close()
        
    except TypeError:
        raise ValueError("file %s not found" % length_file)
    except ValueError:
        raise ValueError("file not formatted correctly, expects two columns gene<tab>length")
    return gene_lengths



def add_species(species, chrs, bed, mrna, premrna):
    
    """

    Creates a dictionary containing all information needed to perform peak calling calcluations 
    for a single species
    
    Paramaters
    -----------
    species: string currently not used
    chrs: list specifying all the chromosomes in a given species
    bed: path to a bed file that contains information on genes (custom file *STRUCTURE_genes.BED.gz)
    mrna: path to a file that contains mRNA lengths (custom CSV file contains gene names follwed by gene lengths)
    premrna: path to a file that contains pre-mRNA lengths (custom CSV file contains gene names follwed by gene lengths_
    
    Returns dict of all items passed to it
    
    TODO:  Add checking to verify that file are actually passed
    """
    par = dict()
    
    #this is non-pythonic, should just combine all lists
    #expand sublists
    par["chrs"] = [item for sublist in chrs for item in sublist] 
    par["gene_bed"] = bed
    par["mRNA"] = mrna
    par["premRNA"] = premrna
    return par
 
def func_star(varables):
    """ covert f([1,2]) to f(1,2) """
    return call_peaks(*varables)


def get_acceptable_species():
    
    """
    
    Finds all species in data directory 
    
    """
    
    acceptable_species = set([])
    for fn in os.listdir(clipper.data_dir()):
        fn = fn.split(".")[0]
        
        if fn == "__init__":
            continue
        
        acceptable_species.add(fn)
    
    return acceptable_species

def build_transcript_data_gtf_as_structure(species, pre_mrna):
    
    """
    
    gtf_file - gtf file generated from AS_STRUCTURE_gtf ipython notebook 
    pre_mrna - if true uses pre mRNA length instead of mRNA length
    
    """
    results = []
    x = clipper.data_file(species + ".AS.STRUCTURE.COMPILED.gff")
    gtf_file = pybedtools.BedTool(x)
    for gene in gtf_file:
        effective_length = gene.attrs['premrna_length'] if pre_mrna else gene.attrs['mrna_length']
        attrs = "gene_id=%s;" % (gene.attrs['gene_id'])
        if "transcript_ids" in gene.attrs:
            attrs += "transcript_ids=%s;" % (gene.attrs['transcript_ids']) 
        attrs += "effective_length=%s" % (str(effective_length)) 
        
        results.append(pybedtools.create_interval_from_list(map(str, [gene['chrom'], 
                                                                      "AS_STRUCTURE", 
                                                                      "mRNA", 
                                                                      str(gene.start + 1), 
                                                                      str(gene.stop + 1),
                                                                      "0", 
                                                                      gene['strand'], 
                                                                      ".",
                                                                      attrs
                                                                      ])))
        
            
    return pybedtools.BedTool(results)

def build_transcript_data_gtf(gtf_file, pre_mrna):
    
    """
    
    Generates GTF file to use when calling genes
    Returns the longest gene from a group of transcripts to call peaks on (this isn't optimal 
    behavior, but until we get a general as structure working its alright)
    
    gtf_file - bedtool from a standard gtf file
    pre_mrna - boolean flag to use pre_mrna instead of mrna 
    
    """
    
    #objects for default dict, no need to test or factor out
    def default_transcript():
        return {'chrom' : None, 'start': np.inf, "stop" : np.NINF, "strand" : None, "gene_id" : None, "mRNA_length" : 0}
    def default_gene():
        return {'start' : 0, 'stop' : 0}
    
    #get all transcripts, their starts, stops and mrna lengths
    transcripts = defaultdict(default_transcript)
    gtf_file = gtf_file.filter(lambda x: x[2] == 'exon')
    for interval in gtf_file:
        cur_transcript = transcripts[interval.attrs['transcript_id']]
        cur_transcript['start'] = min(cur_transcript['start'], interval.start)
        cur_transcript['stop'] = max(cur_transcript['stop'], interval.stop)
        cur_transcript['chrom'] = interval.chrom
        cur_transcript['strand'] = interval.strand
        cur_transcript['gene_id'] = interval.attrs['gene_id']
        cur_transcript['mRNA_length'] += interval.length
        cur_transcript['transcript_id'] = interval.attrs['transcript_id']
        
    #get the longest transcript from each gene group
    longest_genes = defaultdict(default_gene)
    for transcript_name, transcript in transcripts.items():
        cur_gene = transcript['gene_id']
        foo = longest_genes[cur_gene]
        best_length = longest_genes[cur_gene]['stop'] - longest_genes[cur_gene]['start']
        cur_length = transcript['stop'] - transcript['start']
        if  best_length < cur_length:
            longest_genes[cur_gene] = transcript
    
    #convert back into a gtf file 
    results = []
    for gene in longest_genes.values():
        effective_length = gene['stop'] - gene['start'] if pre_mrna else gene['mRNA_length']
        results.append(pybedtools.create_interval_from_list([gene['chrom'], 
                                        "AS_STRUCTURE", 
                                        "mRNA", 
                                        str(gene['start']), 
                                        str(gene['stop']),
                                        "0", 
                                        gene['strand'], 
                                        ".",
                                        "gene_id=" + gene['gene_id'] + "; transcript_id=" + gene['transcript_id'] + "; effective_length=" + str(effective_length)]))
    return pybedtools.BedTool(results)

def build_transcript_data_bed(bed_file, pre_mrna):
    
    """
    
    Generates gene lengths and gene names from BED12 file
    
    bed_file - pybedtools object defining genes
    pre_mrna - flag True indicates use pre-mRNA lengths instead of mRNA lengths 
    
    TODO just turn this totally into a pybedtools object instead of converting 
    it to a dictionary
    """
    raise NotImplementedError("use custom gff file")
    gene_info = {}
    gene_lengths = {}
    
    for line in bed_file:
        
        #builds gene info
        gene_info[line.name] = [line.chrom, line.name, line.start, line.stop, line.strand]
        #builds gene lengths
        
        if pre_mrna:
            gene_lengths[line.name] = line.stop - line.start
        else:
            #Just gets the lengths of the exons (although no mention of cds or not... not important)
            gene_lengths[line.name] = sum([int(x) for x in line[10][:-1].strip().split(",")])
    
    return gene_info, gene_lengths

def build_transcript_data(species, gene_bed, gene_mrna, gene_pre_mrna, pre_mrna):
    
    """
    
    Generates transcript data structures to call peaks on
    
    Allows for either predefined files (from the data directory) 
    or custom files
    
    Accepts species, and genebed, genemrnaand genepremrna options
    
    species - the species to run on
    gene_bed - an abribtary bed file of locations to search for peaks (should be gene locations)
    gene_mrna - the effective length of the mrna of a gene (unmappable regions removed)
    gene_premrna - the effective length of the pre-mrna (unmappable regions removed)
    pre_mrna - flag True indicates use pre-mRNA lengths instead of mRNA lengths
     
    returns genes and lengths dict
    
    """
    
    #error checking 

    acceptable_species = get_acceptable_species()
    if (species is None and 
        gene_bed is None and 
        (gene_mrna is None or gene_pre_mrna is None)):
        
        raise ValueError("You must set either \"species\" or \"geneBed\"+\"geneMRNA\"+\"genePREMRNA\"")

    if species is not None and gene_bed is not None:
        raise ValueError("You shouldn't set both geneBed and species, defaults exist for %s" % (acceptable_species))
    
    #Now actually assign values
    if species is not None:
        try:
            gene_bed      = clipper.data_file(species + ".AS.STRUCTURE_genes.BED.gz")
            gene_mrna     = clipper.data_file(species + ".AS.STRUCTURE_mRNA.lengths")
            gene_pre_mrna = clipper.data_file(species + ".AS.STRUCTURE_premRNA.lengths")

        except ValueError:
            raise ValueError("Defaults don't exist for your species: %s. Please choose from: %s or supply \"geneBed\"+\"geneMRNA\"+\"genePREMRNA\"" % (species, acceptable_species))

    #Selects mRNA or preMRNA lengths
    if pre_mrna is True:
        lenfile = gene_pre_mrna
    else:
        lenfile = gene_mrna

    if lenfile is None:
        raise IOError("""didn't pass correct mRNA length file option 
                    with given length file""")
        
    #builds dict to do processing on,
    genes = build_geneinfo(gene_bed)
    lengths = build_lengths(lenfile)
    
    #this is a stopgap until it can be fully factored out, returing a gtf file of 
    #genes and effective lengths, eventually this is the file we want to pass in
    gtf_list = []
    
    for gene in genes.keys():
        gtf_list.append(pybedtools.create_interval_from_list([genes[gene][0], 
                        "AS_STRUCTURE", 
                        "mRNA",
                        str(genes[gene][2]), 
                        str(genes[gene][3]),
                        ".",
                        str(genes[gene][4]),
                        ".",
                        "gene_id=" + gene + "; effective_length=" + str(lengths[gene])]))

    return pybedtools.BedTool(gtf_list)


def count_transcriptome_reads(results):
    
    """ 
    
    Counts number of reads in the entire transcriptome
    
    results -- the result returned back by call_peaks
    
    returns int, the number of reads in the transcriptome
    
    """
    #count total number of reads in transcriptiome
    transcriptome_reads = 0

    for gene_result in results:
        if gene_result is not None:
            logging.info("nreads: %d" % (gene_result['nreads']))
            transcriptome_reads += gene_result['nreads']

    return transcriptome_reads


def count_transcriptome_length(results):
    transcriptome_length = 0

    for gene_result in results:
        if gene_result is not None:
            transcriptome_length += int(gene_result['loc'].attrs['effective_length'])

    return transcriptome_length


def transcriptome_poissonP(cluster):
    return poissonP(cluster.transcriptome_reads,
                    cluster.number_reads_in_peak,
                    cluster.transcriptome_size,
                    cluster['size'])


def transcript_poissonP(cluster):
    return poissonP(cluster.nreads_in_gene,
                    cluster.number_reads_in_peak,
                    cluster.effective_length,
                    cluster['size'])


def superlocal_poissonP(cluster):
    return poissonP(cluster.area_reads,
                    cluster.number_reads_in_peak,
                    cluster.area_size,
                    cluster['size'])


def write_peak(cluster):
    return "\t".join([str(x) for x in [
        cluster.chrom,
        cluster.genomic_start,
        cluster.genomic_stop,
        cluster.gene_name  + "_" + str(cluster.peak_number) + "_" + str(cluster.number_reads_in_peak),
        cluster.final_p_value,
        cluster.strand,
        cluster.thick_start,
        cluster.thick_stop,
        ]])

def dictify(some_named_tuple):
    return dict((s, getattr(some_named_tuple, s)) for s in some_named_tuple._fields)


def make_peak_df(results):
    peaks = []
    for gene_result in results:
        # alert user that there aren't any clusters for specific gene
        for cluster in gene_result['clusters']:
            peaks.append(dictify(cluster))
    peaks = pd.DataFrame(peaks)
    return peaks


def bh_correct(df):
    """
    :param df:
    :return: returns dataframe wtih adjusted p-value
    """
    df = df.sort_values("final_p_value")
    df['sort_rank'] = np.arange(1, len(df) + 1)
    df['bh_corrected'] = df.apply(lambda x: min(((len(df) / x.sort_rank) * x.final_p_value), 1), axis=1)
    df['padj'] = df.sort_values("final_p_value", ascending=False).bh_corrected.cummin()
    return df.sort_index()

def filter_results(results, poisson_cutoff, transcriptome_size,
                   transcriptome_reads, use_global_cutoff, 
                   bonferroni_correct, algorithm="spline", superlocal=False, min_width=50):
    
    """
    
    Takes a list of results, filters them based off of various argunments and returns only the filtered
    reads
    
    options - the options object from the initial parsing
    poisson_cutoff - user defined possion cutoff (also from options) that filters reads
    results - list of results generated by call_peaks
    transcriptome_size - number of genes there are in the transcriptome
    
    """

    peaks = make_peak_df(results)

    total_clusters = len(peaks)
    if total_clusters == 0:
        print "no peaks detected in dataset"
        return []

    if algorithm == "classic":
        peaks['peak_length'] = peaks['peak_length'].apply(lambda x: max(x, min_width))
    peaks['transcriptome_size'] = transcriptome_size
    peaks['transcriptome_reads'] = transcriptome_reads
    peaks['transcriptome_poisson_p'] = peaks.apply(transcriptome_poissonP, axis=1) if use_global_cutoff else np.nan
    peaks['transcript_poisson_p'] = peaks.apply(transcript_poissonP, axis=1)
    peaks['superlocal_poisson_p'] = peaks.apply(superlocal_poissonP, axis=1) if superlocal else np.nan

    if algorithm == "classic":
        peaks['final_p_value'] = peaks[['transcript_poisson_p', 'superlocal_poisson_p']].max(axis=1)
    else:
        peaks['final_p_value'] = peaks[['transcript_poisson_p', 'superlocal_poisson_p']].min(axis=1)

    if bonferroni_correct:
        peaks = bh_correct(peaks)
        #peaks['final_p_value'] = (peaks['final_p_value'] * total_clusters)

    #This is a bug I should fix, padj isn't getting printed, the uncorreded p-value is
    final_result = peaks[peaks['padj'] < poisson_cutoff]

    return final_result.apply(write_peak, axis=1).values


def mapper(options, line):
    bedtool = pybedtools.BedTool(line, from_string=True)

    #loads in the last bedline, because bedtools doesn't have a .next()
    for bedline in bedtool:
        pass
    
    if options.premRNA:
        length = bedline.stop - bedline.start
    else:
        length = sum([int(x) for x in bedline[10][:-1].strip().split(",")]) #Just gets the lengths of the exons (although no mention of cds or not... not important)
    
    print call_peaks([bedline.chrom, bedline.name, bedline.start, bedline.stop,
                      bedline.strand], length, options.bam, int(options.max_gap), 
                      options.FDR_alpha, options.threshold, 
                      int(options.minreads), options.poisson_cutoff, 
                      options.plotit, 10, 1000, options.SloP, False)


def hadoop_mapper(options):
    
    """
    
    Expermental mapper to give call peaks (running call_peak function) from hadoop
    
    """
    #being lazy for now will make this integrated eventually
   
    for line in sys.stdin:
        mapper(options, line)


def get_exon_bed(species):

    short_species = species.split("_")[0]
    return os.path.join(clipper.data_dir(), "regions", "%s_%s.bed" % (short_species, "exons"))

def main(options):
    
    check_for_index(options.bam)
    
    if options.np == 'autodetect':
        options.np = multiprocessing.cpu_count()

    bamfile = options.bam
    
    if os.path.exists(bamfile):
        #re-set to include the full path to bamfile
        bamfile = os.path.abspath(bamfile) 
        logging.info("bam file is set to %s\n" % (bamfile))
    else:
        logging.error("Bam file: %s is not defined" % (bamfile))
        raise IOError
    if options.gtfFile:
        gene_tool = build_transcript_data_gtf(pybedtools.BedTool(options.gtfFile), options.premRNA).saveas()
    else:
        gene_tool = build_transcript_data_gtf_as_structure(options.species, 
                                                           options.premRNA).saveas()

    #gets all the gene_tool to call peaks on
    if options.gene:
        gene_tool = gene_tool.filter(lambda x: x.attrs['gene_id'] in options.gene)

    #truncates for max gene_tool
    if options.maxgenes:
        print len(gene_tool)
        print options.maxgenes
        print type(options.maxgenes)
        gene_tool = gene_tool.random_subset(int(options.maxgenes))

    exons = get_exon_bed(options.species)

    gene_tool = gene_tool.saveas()

    tasks = [(gene, gene.attrs['effective_length'], bamfile, options.max_gap, options.FDR_alpha,
              options.threshold, options.binom, options.method, options.minreads, options.poisson_cutoff,
              options.plotit, 10, 1000, options.SloP, options.max_width,
              options.min_width, options.algorithm,
              options.reverse_strand, exons) for gene in gene_tool]

    jobs = []
    results = []
    if options.debug:
        for job in tasks:
            print job
            jobs.append(func_star(job))
        
        for job in jobs:
            results.append(job)   
    
    else:
        with closing(multiprocessing.Pool(int(options.np))) as pool:
            jobs = [pool.apply_async(call_peaks, job) for job in tasks]

            for job, task in zip(jobs, tasks):
                try:
                    results.append(job.get(timeout=options.timeout))
                except multiprocessing.TimeoutError as error:
                    logging.error("gene %s timed out" % (task[0].attrs['gene_id']))
                except Exception as error:
                    logging.error("gene %s error for some other reason" % (task[0].attrs['gene_id']))
                    print error
            pool.terminate()

    logging.info("finished with calling peaks")

    if options.save_pickle is True:
        with open(options.outfile + ".all_peaks.pickle", 'w') as pickle_file:  
            pickle.dump(results, file=pickle_file)                
    
    transcriptome_reads = count_transcriptome_reads(results)
    transcriptome_size = count_transcriptome_length(results)

    logging.info("""Transcriptome size is %d, transcriptome reads are %d""" % (transcriptome_size, transcriptome_reads))

    filtered_peaks = filter_results(results, 
                              options.poisson_cutoff, 
                              transcriptome_size,  
                              transcriptome_reads, 
                              options.use_global_cutoff,
                              options.bonferroni_correct,
                              options.algorithm,
                              options.SloP,
                              options.min_width)
            
    outbed = options.outfile

    pybedtools.BedTool("\n".join(filtered_peaks), from_string=True).sort(stream=True).saveas(outbed)

    logging.info("wrote peaks to %s" % (options.outfile))
    
def call_main():
    
    usage = """\npython peakfinder.py -b <bamfile> -s <hg18/hg19/mm9>\n OR 
    \npython peakfinder.py -b <bamfile> --customBED <BEDfile> --customMRNA 
    <mRNA lengths> --customPREMRNA <premRNA lengths>"""
    description = """CLIPper. Michael Lovci, Gabriel Pratt 2012. 
                     CLIP peakfinder that uses fitted smoothing splines to 
                     define clusters of binding.  Computation is performed in
                     parallel using parallelPython. 
                     Refer to: https://github.com/YeoLab/clipper/wiki for instructions. 
                     Questions should be directed to michaeltlovci@gmail.com."""

    parser = OptionParser(usage=usage, description=description)

    parser.add_option("--bam", "-b", dest="bam", help="A bam file to call peaks on", type="string", metavar="FILE.bam")
    parser.add_option("--species", "-s", dest="species", help="A species for your peak-finding, either hg19 or mm9")
    #parser.add_option("--gtfFile", dest="gtfFile", help="use a gtf file instead of the AS structure data")
    parser.add_option("--outfile", "-o", dest="outfile", default="fitted_clusters", help="a bed file output, default:%default")
    parser.add_option("--gene", "-g", dest="gene", action="append", help="A specific gene you'd like try", metavar="GENENAME")
    parser.add_option("--minreads", dest="minreads", help="minimum reads required for a section to start the fitting process.  Default:%default", default=3, type="int", metavar="NREADS")
    #parser.add_option("--premRNA", dest="premRNA", action="store_true", help="use premRNA length cutoff, default:%default", default=False)
    parser.add_option("--poisson-cutoff", dest="poisson_cutoff", type="float", help="p-value cutoff for poisson test, Default:%default", default=0.05, metavar="P")
    parser.add_option("--disable_global_cutoff", dest="use_global_cutoff", action="store_false", help="disables global transcriptome level cutoff to CLIP-seq peaks, Default:On", default=True, metavar="P")
    parser.add_option("--FDR", dest="FDR_alpha", type="float", default=0.05, help="FDR cutoff for significant height estimation, default=%default")
    #parser.add_option("--threshold-method", dest="method", default="binomial", help="Method used for determining height threshold, Can use default=random or binomial")
    parser.add_option("--binomial", dest="binom", type="float", default=0.05, help ="Alpha significance threshold for using Binomial distribution for determining height threshold, default=%default")
    parser.add_option("--threshold", dest="threshold", type="int", default=None, help="Skip FDR calculation and set a threshold yourself")
    parser.add_option("--maxgenes", dest="maxgenes", default=None, type="int", help="stop computation after this many genes, for testing", metavar="NGENES")
    parser.add_option("--processors", dest="np", default="autodetect", help="Number of processors to use. Default: All processors on machine", type="str", metavar="NP")
    #parser.add_option("--superlocal", action="store_true", dest="SloP", default=False, help="Use super-local p-values, counting reads in a 1KB window around peaks")
    parser.add_option("--plot", "-p", dest="plotit", action="store_true", help="make figures of the fits", default=False)
    parser.add_option("--verbose", "-v", dest="verbose", action="store_true", default=False)
    parser.add_option("--quiet", "-q", dest="quiet", action="store_true", default=False, help="suppress notifications")
    parser.add_option("--save-pickle", dest="save_pickle", default=False, action="store_true", help="Save a pickle file containing the analysis")
    parser.add_option("--debug", dest="debug", default=False, action="store_true", help="disables multipcoressing in order to get proper error tracebacks")
    #parser.add_option("--max_width", dest="max_width", type="int", default=75, help="Defines max width for classic algorithm, default: %default")
    #parser.add_option("--min_width", dest="min_width", type="int", default=50, help="Defines min width for classic algorithm, default: %default")
    parser.add_option("--max_gap", dest="max_gap",type="int", default=15, help="defines maximum gap between reads before calling a region a new section, default: %default")
    #parser.add_option("--bonferroni", dest="bonferroni_correct",action="store_true", default=False, help="Perform Bonferroni on data before filtering")
    #parser.add_option("--algorithm", dest="algorithm",default="spline", help="Defines algorithm to run, currently spline, classic, gaussian")
    #parser.add_option("--reverse_strand", dest="reverse_strand",default=False, action="store_true", help="adds option to reverse strand")
    parser.add_option("--timeout", dest="timeout", default=None, type=int, help="adds timeout (in seconds) to genes that take too long (useful for debugging only, or if you don't care about higly expressed genes)")



    (options, args) = parser.parse_args()
    options.premRNA = True
    options.bonferroni_correct = True
    options.algorithm = "spline"
    options.method = "binomial"
    options.gtfFile = None
    if options.plotit:
        options.debug = True

    options.premRNA = True
    options.gtfFile = None
    options.method = "binomial"
    options.SloP = True
    options.bonferroni_correct = True
    options.algorithm = "spline"
    options.reverse_strand = False
    options.max_width = 75
    options.min_width = 50

    #enforces required usage
    if not (options.bam and ((options.species) or (options.gtfFile))): 
        parser.print_help()
        exit()
        
    logging.info("Starting peak calling")
    main(options)


if __name__ == "__main__":
    call_main()
