def equal(*args):
    ''' Check that all arguments are equal.
    '''
    args[0]
    return all(arg == args[0] for arg in args[1:])